from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from code_rook.core.agents.loader import AgentProfileLoader
from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority import RuntimeMode
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.events import (
    LlmRouteSelectedEvent,
    LlmUsageEvent,
    RunFinishedEvent,
    RunStartedEvent,
)
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.compact.compactor import Compactor
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus, EventHandler
from code_rook.core.events.writer import EventWriter
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.factory import create_llm_provider, create_provider_for_route
from code_rook.core.llm.pricing import estimate_cost, get_pricing
from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
from code_rook.core.llm.router import RoutingPolicy, select_route_id
from code_rook.core.loop import AgentLoop
from code_rook.core.lsp import WorkspaceDiagnosticsClient
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.memory import MemoryStore, load_context_file, load_project_instructions
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.persistent_shell import PersistentShellPool
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.prompt_context import build_capability_context, build_runtime_context
from code_rook.core.runs import RUNS_DIR, new_run_id
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.assembly import RuntimeToolAssembly
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.trace.provider import TracingProvider
from code_rook.core.trace.writer import TraceWriter
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.core.worktree import WorktreeManager


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: CodeRookConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        hooks: HookManager | None = None,
        background_registry: BackgroundJobRegistry | None = None,
        workspace_root: Path | None = None,
        subagent_registry: BackgroundTaskRegistry | None = None,
        interaction_manager: InteractionManager | None = None,
        route_registry: RouteRegistry | None = None,
        runtime_service: RuntimeService | None = None,
        process_supervisor: ProcessSupervisor | None = None,
        persistent_shell_pool: PersistentShellPool | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._hooks = hooks or HookManager()
        self._background_registry = background_registry
        self._workspace_boundary = (
            WorkspaceBoundary(workspace_root)
            if workspace_root is not None
            else WorkspaceBoundary.current()
        )
        self._memory_store = MemoryStore(self._workspace_boundary.root / ".coderook" / "memory")
        self._worktree_manager = WorktreeManager(
            self._workspace_boundary.root,
            process_supervisor=process_supervisor,
        )
        self._skill_loader = SkillLoader(self._workspace_boundary.root)
        self._agent_profile_loader = AgentProfileLoader(self._workspace_boundary.root)
        # 跨 run 共享的后台 subagent 任务注册表（可选注入，无注入时自己 new）
        self._task_registry = subagent_registry or BackgroundTaskRegistry()
        self._interaction_manager = interaction_manager
        self._route_registry = route_registry
        self._runtime = runtime_service
        self._artifact_store = ArtifactStore(
            self._workspace_boundary.root / ".coderook" / "artifacts"
        )
        self._diagnostics_client = WorkspaceDiagnosticsClient(
            self._workspace_boundary,
            process_supervisor=process_supervisor,
        )
        self._tool_assembly = RuntimeToolAssembly(
            workspace_boundary=self._workspace_boundary,
            artifact_store=self._artifact_store,
            memory_store=self._memory_store,
            worktree_manager=self._worktree_manager,
            skill_loader=self._skill_loader,
            task_registry=self._task_registry,
            permission_manager=self._permission_manager,
            max_steps=self._config.agent.max_steps,
            runs_dir=self._runs_dir,
            background_registry=self._background_registry,
            interaction_manager=self._interaction_manager,
            mcp_manager=self._mcp_manager,
            route_registry=self._route_registry,
            hooks=self._hooks,
            process_supervisor=process_supervisor,
            persistent_shell_pool=persistent_shell_pool,
        )

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        checkpoint_store: CheckpointStore | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        resolved_route: ResolvedRoute | None = None,
    ) -> ToolRegistry:
        return self._tool_assembly.build(
            task_manager,
            session=session,
            store=store,
            run_id=run_id,
            provider=provider,
            bus=bus,
            child_runs_dir=child_runs_dir,
            session_id=session_id,
            tool_whitelist=tool_whitelist,
            checkpoint_store=checkpoint_store,
            runtime_mode=runtime_mode,
            supports_images=(
                resolved_route.route.supports_images
                if resolved_route is not None
                else True
            ),
        )

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        resolved_route: ResolvedRoute | None = None,
        initial_images: list[dict[str, object]] | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        global_ctx = load_context_file(Path("~/.coderook/context.md").expanduser())
        project_ctx = load_project_instructions(self._workspace_boundary.root)
        recalled = self._memory_store.search(goal, limit=5)
        recalled_context = self._memory_store.format_context(recalled)
        if recalled_context:
            project_ctx = (
                project_ctx.rstrip()
                + "\n\n## Recalled Project Memories\n"
                + recalled_context
            ).strip()

        task_event_sink = (
            self._runtime.task_event_sink(session.id, run_id)
            if self._runtime is not None and session is not None
            else None
        )
        task_manager = TaskManager(run_path / ".tasks", event_sink=task_event_sink)
        checkpoint_store = CheckpointStore(
            run_path / ".checkpoints",
            self._workspace_boundary,
        )

        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            runtime_context=build_runtime_context(self._workspace_boundary.root),
            capability_context=build_capability_context(
                self._skill_loader.list_all_skills(),
                self._agent_profile_loader.list_all(),
            ),
            system_prompt_override=system_prompt_override,
            runtime_mode=runtime_mode,
        )
        for image_block in initial_images or []:
            context.add_pending_image(dict(image_block))
        prompt_decision = (
            await self._hooks.emit(
                "message_submit",
                {"run_id": run_id, "session_id": "", "content": goal},
            )
            if session is None
            else None
        )
        if prompt_decision is not None and prompt_decision.blocked:
            return RunOutcome(
                status="failed",
                result="",
                reason=prompt_decision.reason or "prompt_blocked_by_hook",
            )
        transcript = (
            SessionTranscriptSink(store, session.id, run_id)
            if session is not None and store is not None
            else None
        )

        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            try:
                route_binding = resolved_route
                if route_binding is None and self._route_registry is not None:
                    route_binding = self._route_registry.resolve()
                if self._provider is not None:
                    provider: LLMProvider = self._provider
                elif route_binding is not None:
                    provider = create_provider_for_route(
                        route_binding.route,
                        route_binding.credential,
                    )
                else:
                    provider = create_llm_provider(self._config.llm)
                if route_binding is not None:
                    receipt = route_binding.receipt
                    await bus.publish(
                        LlmRouteSelectedEvent(
                            run_id=run_id,
                            route_id=receipt.route_id,
                            wire_format=receipt.wire_format,
                            base_url_origin=receipt.base_url_origin,
                            model=receipt.model,
                            credential_source=receipt.credential_source,
                            strategy="initial",
                            candidates=(
                                self._route_registry.candidate_ids()
                                if self._route_registry is not None
                                else [receipt.route_id]
                            ),
                            reason="initial_binding",
                            temperature=receipt.temperature,
                            ts=_now(),
                        )
                    )
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                registry = self._build_registry(
                    task_manager,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    checkpoint_store=checkpoint_store,
                    runtime_mode=runtime_mode,
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                compactor = Compactor(
                    bus,
                    session_dir,
                    session_id_str,
                    store=store if session is not None else None,
                    retain_ratio=self._config.compaction.retain_ratio,
                )
                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    compact_threshold=self._config.compaction.auto_threshold,
                    session_id=session_id_str,
                    transcript=transcript,
                    hooks=self._hooks,
                    tool_result_limit=self._config.compaction.tool_result_limit,
                    tool_result_keep=self._config.compaction.tool_result_keep,
                    tool_result_summarize_threshold=(
                        self._config.compaction.tool_result_summarize_threshold
                    ),
                    todo_state=task_manager,
                    interaction_manager=self._interaction_manager,
                    artifact_store=self._artifact_store,
                    diagnostics_client=self._diagnostics_client,
                    escalate_plan_thinking=(
                        route_binding is not None
                        and route_binding.route.thinking != "off"
                    ),
                    auto_step_continues=self._config.agent.max_step_continues,
                    route_refresher=self._make_route_refresher(
                        bus=bus,
                        run_id=run_id,
                        runtime_mode=runtime_mode,
                        trace=self._trace,
                        initial_binding=route_binding,
                    ),
                )
                previous_authority = None
                permission_manager = self._permission_manager
                if permission_manager is not None and session_id_str:
                    previous_authority = permission_manager.get_authority_snapshot(session_id_str)
                    permission_manager.set_authority_snapshot(
                        session_id_str,
                        previous_authority.model_copy(update={"mode": runtime_mode}),
                    )
                if self._interaction_manager is not None:
                    self._interaction_manager.register_run(run_id)
                try:
                    await loop.run(context)
                finally:
                    if self._interaction_manager is not None:
                        self._interaction_manager.unregister_run(run_id)
                    if previous_authority is not None:
                        assert permission_manager is not None
                        permission_manager.set_authority_snapshot(
                            session_id_str,
                            previous_authority,
                        )
                if context.status == "success":
                    self._memory_store.remember_explicit_prompt(
                        goal,
                        source_session_id=session_id_str,
                        source_run_id=run_id,
                    )
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            # 后台 subagent 由 daemon 级 registry 管理生命周期，不在此处清理。
            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if session is not None and store is not None:
            store.recover_incomplete_tail(session.id)

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )

    # 构造 per-turn 路由刷新回调：仅在配置了非 static 路由且存在 route registry 时启用；
    # 每步根据策略选目标路由，模型或路由发生变化时重建 provider 并重新发布 route_selected 事件
    def _make_route_refresher(
        self,
        *,
        bus: EventBus,
        run_id: str,
        runtime_mode: RuntimeMode,
        trace: TraceWriter | None,
        initial_binding: ResolvedRoute | None,
    ) -> Callable[[int], Awaitable[LLMProvider | None]] | None:
        llm = self._config.llm
        if self._route_registry is None or llm.router == "static":
            return None
        policy = RoutingPolicy(
            strategy=llm.router,
            plan_route_id=llm.router_plan_route,
            act_route_id=llm.router_act_route,
            cost_budget_usd=llm.router_cost_budget,
            cost_fallback_route_id=llm.router_cost_fallback,
        )
        # 记录当前绑定键（route_id + model）与累计成本，供每步决策增量比较
        current_key: str = self._binding_key(initial_binding)
        accumulated_cost: float = 0.0

        # 无 durable runtime 的独立 runner 才订阅内存用量，生产 daemon 读取统一 usage 投影
        async def _on_usage(event: object) -> None:
            nonlocal accumulated_cost
            if not isinstance(event, LlmUsageEvent) or event.run_id != run_id:
                return
            pricing = get_pricing(event.model or llm.default_model)
            if pricing is None:
                return
            accumulated_cost += estimate_cost(
                pricing,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_tokens=event.cache_read_input_tokens,
                cache_write_tokens=event.cache_creation_input_tokens,
            )

        if policy.strategy == "cost_budget" and self._runtime is None:
            bus.subscribe(_on_usage)

        # 在每步 provider.chat 前被 loop 调用；返回新 provider 表示需要切换
        async def _refresh(_step: int) -> LLMProvider | None:
            nonlocal current_key, accumulated_cost
            registry = self._route_registry
            if registry is None:
                return None
            if policy.strategy == "cost_budget" and self._runtime is not None:
                durable_cost = await self._runtime.get_estimated_cost(run_id)
                if durable_cost is not None:
                    accumulated_cost = durable_cost
            res = registry.resolve()
            target_route_id = select_route_id(
                policy,
                mode=runtime_mode,
                step=_step,
                cost_usd=accumulated_cost,
            )
            binding = res
            if target_route_id is not None and target_route_id != res.route.id:
                binding = registry.resolve(target_route_id)
            new_key = f"{binding.route.id}:{binding.route.model}"
            receipt = binding.receipt
            if policy.strategy == "cost_budget":
                reason = (
                    "cost_budget_exceeded"
                    if target_route_id is not None
                    else "cost_budget_within_limit"
                )
            elif policy.strategy == "rule_based":
                reason = (
                    "rule_based_plan"
                    if runtime_mode == RuntimeMode.PLAN and target_route_id is not None
                    else "rule_based_act"
                    if target_route_id is not None
                    else "rule_based_active_fallback"
                )
            else:
                reason = "active_route"
            await bus.publish(
                LlmRouteSelectedEvent(
                    run_id=run_id,
                    route_id=receipt.route_id,
                    wire_format=receipt.wire_format,
                    base_url_origin=receipt.base_url_origin,
                    model=receipt.model,
                    credential_source=receipt.credential_source,
                    strategy=policy.strategy,
                    candidates=registry.candidate_ids(),
                    reason=reason,
                    step=_step,
                    accumulated_cost_usd=accumulated_cost,
                    cost_budget_usd=(
                        policy.cost_budget_usd
                        if policy.strategy == "cost_budget"
                        else None
                    ),
                    temperature=receipt.temperature,
                    ts=_now(),
                )
            )
            if new_key == current_key:
                return None
            current_key = new_key
            provider = create_provider_for_route(binding.route, binding.credential)
            if trace is not None:
                provider = TracingProvider(
                    provider,
                    trace,
                    include_payload=self._config.trace.include_llm_payload,
                )
            return provider

        return _refresh

    # 生成初始绑定的键；无绑定（纯配置路径）用空键占位
    @staticmethod
    def _binding_key(binding: ResolvedRoute | None) -> str:
        if binding is None:
            return ""
        return f"{binding.route.id}:{binding.route.model}"

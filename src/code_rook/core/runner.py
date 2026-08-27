from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from code_rook.core.agents.loader import AgentProfileLoader
from code_rook.core.artifacts import ArtifactStore
from code_rook.core.audit import AuditHealth
from code_rook.core.authority import AuthoritySnapshot, RuntimeMode, WorkspaceTrust
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.events import (
    ContextRepositoryEvent,
    LlmRouteSelectedEvent,
    RunFailureCategory,
    RunFinishedEvent,
    RunOutcomeStatus,
    RunPhaseChangedEvent,
    RunStartedEvent,
    StrategyProposedEvent,
    StrategyResolvedEvent,
    TaskProfiledEvent,
    ToolCallStartedEvent,
    VerificationCompletedEvent,
    VerificationFailedEvent,
)
from code_rook.core.capabilities import (
    CapabilityContribution,
    CapabilityKernel,
    CapabilityKind,
    CapabilityScope,
    CapabilityStability,
    ContributionHandle,
)
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.compact.compactor import Compactor
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus, EventHandler
from code_rook.core.events.writer import EventWriter
from code_rook.core.execution import SessionLedgerBridge
from code_rook.core.features import labs_enabled
from code_rook.core.goal import GoalBudgetProvider, GoalService
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.factory import create_llm_provider, create_provider_for_route
from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
from code_rook.core.llm.router import RoutingPolicy, select_route_id
from code_rook.core.loop import AgentLoop
from code_rook.core.lsp import WorkspaceDiagnosticsClient
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.memory import MemoryStore, load_context_file, load_project_instructions
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.persistent_shell import PersistentShellPool
from code_rook.core.presets import get_agent_preset
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.prompt_context import build_capability_context, build_runtime_context
from code_rook.core.repository import RepositoryIndex
from code_rook.core.runs import RUNS_DIR, new_run_id
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.strategy import TaskProfile, TaskStrategy, TaskStrategyRouter
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.assembly import RuntimeToolAssembly
from code_rook.core.tools.builtin.ask_user_question import AskUserQuestionTool
from code_rook.core.tools.families.control import UpdatePlanTool
from code_rook.core.tools.program import RunToolProgram
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


class _NullAsyncContext:
    # 返回空上下文自身，统一无 Session 运行和 Session 运行的资源装配
    async def __aenter__(self) -> _NullAsyncContext:
        return self

    # 空上下文退出时不执行任何清理
    async def __aexit__(self, *args: object) -> None:
        return None


# 从冻结路由和权限快照生成模型请求可引用的执行契约元数据
def _request_metadata(
    route: ResolvedRoute | None,
    authority: AuthoritySnapshot,
    *,
    runtime_mode: RuntimeMode,
    preset_id: str = "standard",
    preset_digest: str = "",
    task_profile: TaskProfile | None = None,
) -> dict[str, object]:
    contract = {
        "runtime_mode": runtime_mode.value,
        "authority": authority.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        json.dumps(
            contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    receipt = route.receipt if route is not None else None
    return {
        "route_id": receipt.route_id if receipt is not None else "",
        "model": receipt.model if receipt is not None else "",
        "wire_format": receipt.wire_format if receipt is not None else "",
        "execution_contract_digest": digest,
        "preset_id": preset_id,
        "preset_digest": preset_digest,
        "task_profile": (
            task_profile.model_dump(mode="json") if task_profile is not None else {}
        ),
        "task_profile_digest": task_profile.digest if task_profile is not None else "",
    }


# 将内部运行状态归一为公开结果状态和安全失败类别
def _public_run_outcome(
    status: str,
    reason: str | None,
) -> tuple[RunOutcomeStatus, RunFailureCategory | None]:
    if status == "success":
        return "completed", None
    normalized = (reason or "failed").strip().lower()
    if normalized == "cancelled":
        return "cancelled", "user_cancelled"
    if normalized == "incomplete":
        return "incomplete", "model"
    if normalized == "content_filtered":
        return "content_filtered", "model"
    if normalized == "exceeded_max_steps":
        return "failed", "model"
    if normalized in {"transport_error", "stream_idle_timeout", "stream_wall_timeout"}:
        return "transport_error", "network"
    if normalized in {"permission_denied", "permission_timeout"}:
        return "failed", "permission"
    if normalized == "route_capability_error":
        return "failed", "configuration"
    if normalized.startswith("sandbox"):
        return "failed", "sandbox"
    return "failed", "runtime"


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
        goal_service: GoalService | None = None,
        audit_health: AuditHealth | None = None,
        capability_kernel: CapabilityKernel | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._background_registry = background_registry
        self._workspace_boundary = (
            WorkspaceBoundary(workspace_root)
            if workspace_root is not None
            else WorkspaceBoundary.current()
        )
        self._capability_kernel = capability_kernel or CapabilityKernel()
        self._workspace_capability_scope = CapabilityScope(
            workspace=str(self._workspace_boundary.root.resolve())
        )
        resolved_hooks = self._capability_kernel.resolve(
            CapabilityKind.HOOK_MANAGER,
            "default",
            self._workspace_capability_scope,
        )
        resolved_mcp = self._capability_kernel.resolve(
            CapabilityKind.MCP_MANAGER,
            "default",
            self._workspace_capability_scope,
        )
        resolved_routes = self._capability_kernel.resolve(
            CapabilityKind.PROVIDER_CATALOG,
            "default",
            self._workspace_capability_scope,
        )
        self._mcp_manager = (
            resolved_mcp if isinstance(resolved_mcp, McpServerManager) else mcp_manager
        )
        self._hooks = (
            resolved_hooks
            if isinstance(resolved_hooks, HookManager)
            else hooks or HookManager()
        )
        self._memory_store = MemoryStore(self._workspace_boundary.root / ".coderook" / "memory")
        self._repository_index = RepositoryIndex(self._workspace_boundary)
        self._worktree_manager = WorktreeManager(
            self._workspace_boundary.root,
            process_supervisor=process_supervisor,
        )
        self._skill_loader = SkillLoader(self._workspace_boundary.root)
        self._agent_profile_loader = AgentProfileLoader(self._workspace_boundary.root)
        # 跨 run 共享的后台 subagent 任务注册表（可选注入，无注入时自己 new）
        self._task_registry = subagent_registry or BackgroundTaskRegistry()
        self._interaction_manager = interaction_manager
        self._route_registry = (
            resolved_routes
            if isinstance(resolved_routes, RouteRegistry)
            else route_registry
        )
        self._runtime = runtime_service
        self._goal_service = goal_service
        self._audit_health = audit_health
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
            repository_index=self._repository_index,
            goal_service=goal_service,
            hooks=self._hooks,
            process_supervisor=process_supervisor,
            persistent_shell_pool=persistent_shell_pool,
            env_overlay=self._config.llm.credential_overlay,
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
        authority_snapshot: AuthoritySnapshot | None = None,
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
            authority_snapshot=authority_snapshot,
            supports_images=(
                resolved_route.route.supports_images
                if resolved_route is not None
                else True
            ),
            route_binding=resolved_route,
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
        resolved_route_is_explicit: bool = False,
        initial_images: list[dict[str, object]] | None = None,
        persistent_goal_context: str = "",
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        agent_preset = None
        if session is not None:
            agent_preset = get_agent_preset(session.preset_id)
            if session.preset_digest != agent_preset.digest:
                raise ValueError("session preset digest changed; fork or repair the session")
            if agent_preset.tool_program and not labs_enabled():
                raise ValueError("tool-program preset requires CODEROOK_LABS=1")
            if agent_preset.tool_allowlist is not None:
                requested_tools = set(tool_whitelist or agent_preset.tool_allowlist)
                tool_whitelist = sorted(
                    requested_tools & set(agent_preset.tool_allowlist)
                )
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)
        session_id_str = session.id if session is not None else ""
        permission_manager = self._permission_manager
        turn_authority = (
            permission_manager.get_authority_snapshot(session_id_str).model_copy(
                update={"mode": runtime_mode}
            )
            if permission_manager is not None and session_id_str
            else AuthoritySnapshot(mode=runtime_mode)
        )
        workspace_trusted = turn_authority.workspace_trust == WorkspaceTrust.TRUSTED

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
                self._skill_loader.list_for_execution(
                    workspace_trusted=workspace_trusted
                ),
                self._agent_profile_loader.list_for_execution(
                    workspace_trusted=workspace_trusted
                ),
            ),
            persistent_goal_context=persistent_goal_context,
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
        repository_selection = await asyncio.to_thread(
            self._repository_index.select_context,
            goal,
        )
        context.repository_context = repository_selection.content
        context.repository_context_metadata = {
            "repository_hash": repository_selection.repository_hash,
            "budget_chars": repository_selection.budget_chars,
            "used_chars": repository_selection.used_chars,
            "paths": list(repository_selection.paths),
            "selection_reasons": list(repository_selection.reasons),
            "cache_hits": repository_selection.cache_hits,
            "parsed_files": repository_selection.parsed_files,
        }
        transcript = (
            SessionTranscriptSink(store, session.id, run_id)
            if session is not None and store is not None
            else None
        )

        ledger_bridge = (
            SessionLedgerBridge(
                store,
                session.id,
                run_id=run_id,
                audit_health=self._audit_health,
            )
            if session is not None and store is not None
            else None
        )
        async with (
            EventWriter(
                run_path / "events.jsonl",
                audit_health=self._audit_health,
            ) as writer,
            ledger_bridge if ledger_bridge is not None else _NullAsyncContext() as active_ledger,
        ):
            writer.subscribe(bus)
            if isinstance(active_ledger, SessionLedgerBridge):
                active_ledger.subscribe(bus)
            tracked_phase = ""

            # 根据可信工具与验证事件发布唯一阶段信号，TUI 不再自行猜测运行状态
            async def publish_runtime_phase(event: object) -> None:
                nonlocal tracked_phase
                phase: Literal["exploring", "executing", "verifying"] | None = None
                summary = ""
                if isinstance(event, (VerificationCompletedEvent, VerificationFailedEvent)):
                    phase = "verifying"
                    summary = "正在核对验证结果"
                elif isinstance(event, ToolCallStartedEvent):
                    action = str(event.params.get("action", "")).casefold()
                    if event.tool_name in {"read_file", "list_dir", "grep", "glob"} or action in {
                        "read",
                        "list",
                        "search",
                        "search_name",
                        "search_content",
                        "status",
                        "diff",
                    }:
                        phase = "exploring"
                        summary = "正在读取和定位相关代码"
                    elif event.tool_name not in {"update_plan", "ask_user_question"}:
                        phase = "executing"
                        summary = "正在执行已冻结的任务策略"
                if phase is None or phase == tracked_phase:
                    return
                tracked_phase = phase
                await bus.publish(
                    RunPhaseChangedEvent(
                        run_id=run_id,
                        phase=phase,
                        current={"exploring": 2, "executing": 5, "verifying": 6}[phase],
                        total=8,
                        summary=summary,
                        ts=_now(),
                    )
                )

            bus.subscribe(publish_runtime_phase)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))
            await bus.publish(
                RunPhaseChangedEvent(
                    run_id=run_id,
                    phase="understanding",
                    current=1,
                    total=8,
                    summary="正在理解任务边界和风险",
                    ts=_now(),
                )
            )
            await bus.publish(
                ContextRepositoryEvent(
                    run_id=run_id,
                    repository_hash=repository_selection.repository_hash,
                    budget_chars=repository_selection.budget_chars,
                    used_chars=repository_selection.used_chars,
                    paths=list(repository_selection.paths),
                    selection_reasons=list(repository_selection.reasons),
                    cache_hits=repository_selection.cache_hits,
                    parsed_files=repository_selection.parsed_files,
                    ts=_now(),
                )
            )

            cancelled = False
            plan_approval_pending = False
            try:
                route_binding = await self.resolve_turn_binding(
                    resolved_route=resolved_route,
                    resolved_route_is_explicit=resolved_route_is_explicit,
                    runtime_mode=runtime_mode,
                    run_id=run_id,
                )
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
                if self._goal_service is not None and session_id_str:
                    active_goal = self._goal_service.current(session_id_str)
                    if (
                        active_goal is not None
                        and active_goal.current_run_id == run_id
                        and active_goal.token_budget is not None
                    ):
                        provider = GoalBudgetProvider(
                            provider,
                            self._goal_service,
                            active_goal.id,
                        )
                task_profile = await TaskStrategyRouter().classify(
                    goal,
                    provider=provider if self._provider is None else None,
                    runtime_mode=runtime_mode,
                    preset_id=agent_preset.id if agent_preset is not None else "standard",
                    run_id=run_id,
                    method=self._config.agent.task_router,
                    delegation_policy=self._config.agent.delegation_policy,
                )
                await bus.publish(
                    TaskProfiledEvent(
                        run_id=run_id,
                        profile=task_profile.model_dump(mode="json"),
                        profile_digest=task_profile.digest,
                        ts=_now(),
                    )
                )
                await bus.publish(
                    StrategyProposedEvent(
                        run_id=run_id,
                        profile_digest=task_profile.digest,
                        strategy=task_profile.strategy.value,
                        summary=task_profile.user_summary,
                        ts=_now(),
                    )
                )
                context.runtime_context = (
                    context.runtime_context.rstrip()
                    + "\n\n## Frozen Task Strategy\n"
                    + json.dumps(
                        task_profile.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\nFollow this frozen strategy. For plan_first, establish an explicit "
                    "bounded plan before mutation. For delegate, call agent.validate_plan before "
                    "starting any worker; if validation fails, continue as one agent."
                )
                turn_authority_active = False
                if permission_manager is not None and session_id_str:
                    permission_manager.begin_turn(
                        session_id_str,
                        turn_authority,
                    )
                    turn_authority_active = True
                tool_contribution: ContributionHandle | None = None
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                try:
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
                        resolved_route=route_binding,
                        authority_snapshot=turn_authority,
                    )
                    normal_tool_allowlist = task_profile.model_tool_allowlist()
                    normal_action_allowlist = task_profile.model_action_allowlist()
                    plan_gate_active = (
                        task_profile.strategy == TaskStrategy.PLAN_FIRST
                        and runtime_mode == RuntimeMode.ACT
                    )
                    question_tool = registry.get("ask_user_question")
                    clarification_gate_active = (
                        task_profile.confidence < 0.75
                        and runtime_mode == RuntimeMode.ACT
                        and isinstance(question_tool, AskUserQuestionTool)
                    )
                    if clarification_gate_active:
                        registry.set_model_tool_allowlist(
                            frozenset({"ask_user_question"})
                        )
                        registry.set_model_action_allowlist({})
                    else:
                        registry.set_model_tool_allowlist(
                            task_profile.planning_tool_allowlist()
                            if plan_gate_active
                            else normal_tool_allowlist
                        )
                        registry.set_model_action_allowlist(
                            task_profile.planning_action_allowlist()
                            if plan_gate_active
                            else normal_action_allowlist
                        )
                    plan_tool = registry.get("update_plan")
                    if plan_gate_active and isinstance(plan_tool, UpdatePlanTool):

                        # 计划持久化后等待会话审批，当前 Turn 始终保持只读目录
                        async def resolve_plan_gate(ticket: str) -> None:
                            nonlocal plan_approval_pending
                            plan_approval_pending = True
                            await bus.publish(
                                RunPhaseChangedEvent(
                                    run_id=run_id,
                                    phase="waiting_confirmation",
                                    current=4,
                                    total=8,
                                    summary=(
                                        "计划已记录，等待用户批准；当前 Turn 不开放修改工具"
                                    ),
                                    ts=_now(),
                                )
                            )

                        plan_tool.set_ticket_handler(resolve_plan_gate)
                        if not clarification_gate_active:
                            await bus.publish(
                                RunPhaseChangedEvent(
                                    run_id=run_id,
                                    phase="planning",
                                    current=3,
                                    total=8,
                                    summary="只读探索并形成可执行计划",
                                    ts=_now(),
                                )
                            )
                    else:
                        if plan_gate_active:
                            registry.set_model_tool_allowlist(normal_tool_allowlist)
                            registry.set_model_action_allowlist(normal_action_allowlist)
                        if not clarification_gate_active:
                            await bus.publish(
                                StrategyResolvedEvent(
                                    run_id=run_id,
                                    profile_digest=task_profile.digest,
                                    strategy=task_profile.strategy.value,
                                    reason="frozen_task_profile",
                                    ts=_now(),
                                )
                            )
                            await bus.publish(
                                RunPhaseChangedEvent(
                                    run_id=run_id,
                                    phase=(
                                        "exploring"
                                        if task_profile.risk.value == "read"
                                        else "executing"
                                    ),
                                    current=(
                                        2 if task_profile.risk.value == "read" else 5
                                    ),
                                    total=8,
                                    summary=task_profile.user_summary,
                                    ts=_now(),
                                )
                            )
                    if clarification_gate_active and isinstance(
                        question_tool,
                        AskUserQuestionTool,
                    ):

                        # 用户回答后只推进到画像允许的下一层门禁，不直接扩大修改权限
                        async def resolve_clarification_gate(answer: str) -> None:
                            nonlocal task_profile
                            nonlocal normal_action_allowlist
                            nonlocal normal_tool_allowlist
                            clarified = await TaskStrategyRouter().classify(
                                f"{goal}\n\nUser clarification: {answer}",
                                runtime_mode=runtime_mode,
                                preset_id=(
                                    agent_preset.id
                                    if agent_preset is not None
                                    else "standard"
                                ),
                                method="rules_only",
                                delegation_policy=self._config.agent.delegation_policy,
                            )
                            if plan_gate_active:
                                clarified = clarified.model_copy(
                                    update={
                                        "strategy": TaskStrategy.PLAN_FIRST,
                                        "delegation_allowed": False,
                                        "signals": tuple(
                                            (*clarified.signals, "clarification_answered")
                                        ),
                                        "source": "rules_clarified",
                                        "digest": "",
                                    }
                                ).with_digest()
                            task_profile = clarified
                            normal_tool_allowlist = task_profile.model_tool_allowlist()
                            normal_action_allowlist = task_profile.model_action_allowlist()
                            context.runtime_context = (
                                context.runtime_context.rstrip()
                                + "\n\n## Clarified Task Strategy\n"
                                + json.dumps(
                                    task_profile.model_dump(mode="json"),
                                    ensure_ascii=False,
                                    sort_keys=True,
                                )
                            )
                            await bus.publish(
                                TaskProfiledEvent(
                                    run_id=run_id,
                                    profile=task_profile.model_dump(mode="json"),
                                    profile_digest=task_profile.digest,
                                    ts=_now(),
                                )
                            )
                            await bus.publish(
                                StrategyProposedEvent(
                                    run_id=run_id,
                                    profile_digest=task_profile.digest,
                                    strategy=task_profile.strategy.value,
                                    summary=task_profile.user_summary,
                                    ts=_now(),
                                )
                            )
                            if plan_gate_active:
                                registry.set_model_tool_allowlist(
                                    task_profile.planning_tool_allowlist()
                                )
                                registry.set_model_action_allowlist(
                                    task_profile.planning_action_allowlist()
                                )
                            else:
                                registry.set_model_tool_allowlist(normal_tool_allowlist)
                                registry.set_model_action_allowlist(normal_action_allowlist)
                                await bus.publish(
                                    StrategyResolvedEvent(
                                        run_id=run_id,
                                        profile_digest=task_profile.digest,
                                        strategy=task_profile.strategy.value,
                                        reason="clarification_answered",
                                        ts=_now(),
                                    )
                                )
                            await bus.publish(
                                RunPhaseChangedEvent(
                                    run_id=run_id,
                                    phase="planning" if plan_gate_active else "exploring",
                                    current=3 if plan_gate_active else 2,
                                    total=8,
                                    summary=(
                                        "已收到澄清，继续只读探索并形成计划"
                                        if plan_gate_active
                                        else "已收到澄清，继续处理任务"
                                    ),
                                    ts=_now(),
                                )
                            )

                        question_tool.set_answer_handler(resolve_clarification_gate)
                        await bus.publish(
                            RunPhaseChangedEvent(
                                run_id=run_id,
                                phase="waiting_confirmation",
                                current=4,
                                total=8,
                                summary="任务边界不明确，需要一个简短澄清",
                                ts=_now(),
                            )
                        )
                    tool_scope = CapabilityScope(
                        workspace=self._workspace_capability_scope.workspace,
                        session=session_id_str or run_id,
                    )
                    tool_contribution = self._capability_kernel.register(
                        CapabilityContribution(
                            id="default",
                            kind=CapabilityKind.TOOL_REGISTRY.value,
                            provider=registry,
                            stability=CapabilityStability.STABLE,
                            scope=tool_scope,
                        )
                    )
                    resolved_registry = self._capability_kernel.resolve(
                        CapabilityKind.TOOL_REGISTRY,
                        "default",
                        tool_scope,
                    )
                    if not isinstance(resolved_registry, ToolRegistry):
                        raise RuntimeError("session tool registry capability is unavailable")
                    registry = resolved_registry
                    if agent_preset is not None and agent_preset.tool_program:
                        registry.register(
                            RunToolProgram(
                                registry,
                                bus,
                                run_id,
                                session_id=session_id_str,
                                permission_manager=self._permission_manager,
                                hooks=self._hooks,
                                artifact_store=self._artifact_store,
                                authority_snapshot=turn_authority,
                                step_provider=lambda: context.step,
                            )
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
                        strategy=self._config.compaction.strategy,
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
                        supports_tools=(
                            route_binding.route.supports_tools
                            if route_binding is not None
                            else True
                        ),
                        supports_parallel_tools=(
                            route_binding.route.supports_parallel_tools
                            if route_binding is not None
                            else True
                        ),
                        supports_images=(
                            route_binding.route.supports_images
                            if route_binding is not None
                            else True
                        ),
                        auto_step_continues=self._config.agent.max_step_continues,
                        authority_snapshot=turn_authority,
                        request_metadata=_request_metadata(
                            route_binding,
                            turn_authority,
                            runtime_mode=runtime_mode,
                            preset_id=agent_preset.id if agent_preset is not None else "standard",
                            preset_digest=agent_preset.digest if agent_preset is not None else "",
                            task_profile=task_profile,
                        ),
                    )
                    if self._interaction_manager is not None:
                        self._interaction_manager.register_run(run_id)
                    await loop.run(context)
                finally:
                    if tool_contribution is not None:
                        tool_contribution.dispose()
                    if self._interaction_manager is not None:
                        self._interaction_manager.unregister_run(run_id)
                    if turn_authority_active:
                        assert permission_manager is not None
                        permission_manager.end_turn(session_id_str)
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
            public_outcome, failure_category = _public_run_outcome(
                context.status,
                context.reason,
            )
            if plan_approval_pending and context.status == "success":
                public_outcome = "incomplete"
                failure_category = None
            await bus.publish(
                RunPhaseChangedEvent(
                    run_id=run_id,
                    phase=(
                        "waiting_confirmation"
                        if plan_approval_pending and context.status == "success"
                        else "completed"
                        if public_outcome == "completed"
                        else "interrupted"
                        if public_outcome == "cancelled"
                        else "failed"
                    ),
                    current=8,
                    total=8,
                    summary=(
                        "plan_approval_required"
                        if plan_approval_pending and context.status == "success"
                        else context.reason or context.status
                    ),
                    ts=_now(),
                )
            )
            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    outcome=public_outcome,
                    failure_category=failure_category,
                    result_summary=context.result.strip()[:4_000] or None,
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

    # 在 Turn 启动前选择并冻结完整路由绑定，运行中配置变化只影响下一 Turn
    async def resolve_turn_binding(
        self,
        *,
        resolved_route: ResolvedRoute | None,
        resolved_route_is_explicit: bool = False,
        runtime_mode: RuntimeMode,
        run_id: str,
    ) -> ResolvedRoute | None:
        if resolved_route is not None and resolved_route_is_explicit:
            return resolved_route
        registry = self._route_registry
        if registry is None:
            return resolved_route
        active = resolved_route or registry.resolve()
        llm = self._config.llm
        if llm.router == "static":
            return active
        policy = RoutingPolicy(
            strategy=llm.router,
            plan_route_id=llm.router_plan_route,
            act_route_id=llm.router_act_route,
            cost_budget_usd=llm.router_cost_budget,
            cost_fallback_route_id=llm.router_cost_fallback,
        )
        accumulated_cost: float = 0.0
        if policy.strategy == "cost_budget" and self._runtime is not None:
            durable_cost = await self._runtime.get_estimated_cost(run_id)
            if durable_cost is not None:
                accumulated_cost = durable_cost
        target_route_id = select_route_id(
            policy,
            mode=runtime_mode,
            step=1,
            cost_usd=accumulated_cost,
        )
        if target_route_id is None or target_route_id == active.route.id:
            return active
        return registry.resolve(target_route_id)

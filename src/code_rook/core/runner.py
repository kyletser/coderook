from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.events import RunFinishedEvent, RunStartedEvent
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.compact.compactor import Compactor
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus, EventHandler
from code_rook.core.events.writer import EventWriter
from code_rook.core.hooks import HookManager
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.factory import create_llm_provider
from code_rook.core.loop import AgentLoop
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.memory import MemoryStore, load_context_file
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runs import RUNS_DIR, new_run_id
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.tool import AgentResultTool, SpawnAgentTool
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.builtin import (
    ApplyPatchTool,
    BackgroundCancelTool,
    BackgroundListTool,
    BackgroundResultTool,
    BackgroundStartTool,
    BashTool,
    CheckpointListTool,
    CheckpointRewindTool,
    EditFileTool,
    GitDiffTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    MemoryForgetTool,
    MemorySaveTool,
    MemorySearchTool,
    NoteSaveTool,
    ReadFileTool,
    TaskClaimTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WorktreeCreateTool,
    WorktreeListTool,
    WorktreeRemoveTool,
    WriteFileTool,
)
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
        self._worktree_manager = WorktreeManager(self._workspace_boundary.root)
        # 跨 run 共享的后台 subagent 任务注册表（可选注入，无注入时自己 new）
        self._task_registry = subagent_registry or BackgroundTaskRegistry()

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
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        for t in [
            ReadFileTool(self._workspace_boundary),
            GlobTool(self._workspace_boundary),
            GrepTool(self._workspace_boundary),
            GitDiffTool(self._workspace_boundary),
            BashTool(self._workspace_boundary.root),
            EditFileTool(
                self._workspace_boundary,
                checkpoint_store=checkpoint_store,
            ),
            ApplyPatchTool(
                self._workspace_boundary,
                checkpoint_store=checkpoint_store,
            ),
            WriteFileTool(
                self._workspace_boundary,
                checkpoint_store=checkpoint_store,
            ),
            ListDirTool(self._workspace_boundary),
        ]:
            if _ok(t.name):
                registry.register(t)
        if checkpoint_store is not None:
            for checkpoint_tool in [
                CheckpointListTool(checkpoint_store),
                CheckpointRewindTool(checkpoint_store),
            ]:
                if _ok(checkpoint_tool.name):
                    registry.register(checkpoint_tool)
        for t in [
            TaskCreateTool(task_manager),
            TaskClaimTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        for memory_tool in [
            MemorySaveTool(self._memory_store, session_id, run_id or ""),
            MemorySearchTool(self._memory_store),
            MemoryForgetTool(self._memory_store),
        ]:
            if _ok(memory_tool.name):
                registry.register(memory_tool)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if _ok("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._config.agent.max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        workspace_boundary=self._workspace_boundary,
                        task_manager=task_manager,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        if self._background_registry is not None:
            for background_tool in [
                BackgroundStartTool(self._background_registry, session_id, run_id or ""),
                BackgroundResultTool(self._background_registry),
                BackgroundListTool(self._background_registry, session_id),
                BackgroundCancelTool(self._background_registry),
            ]:
                if _ok(background_tool.name):
                    registry.register(background_tool)
        for worktree_tool in [
            WorktreeCreateTool(self._worktree_manager),
            WorktreeListTool(self._worktree_manager),
            WorktreeRemoveTool(self._worktree_manager),
        ]:
            if _ok(worktree_tool.name):
                registry.register(worktree_tool)
        return registry

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
        project_ctx = load_context_file(Path(".coderook/context.md"))
        recalled = self._memory_store.search(goal, limit=5)
        recalled_context = self._memory_store.format_context(recalled)
        if recalled_context:
            project_ctx = (
                project_ctx.rstrip()
                + "\n\n## Recalled Project Memories\n"
                + recalled_context
            ).strip()

        task_manager = TaskManager(run_path / ".tasks")
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
            system_prompt_override=system_prompt_override,
        )
        prompt_decision = await self._hooks.emit(
            "UserPromptSubmit",
            {"run_id": run_id, "session_id": session.id if session else "", "prompt": goal},
        )
        if prompt_decision.blocked:
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
                provider: LLMProvider = self._provider or create_llm_provider(self._config.llm)
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
                )
                await loop.run(context)
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

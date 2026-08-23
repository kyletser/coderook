from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import signal
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import code_rook
from code_rook.core.agents.loader import AgentProfileLoader
from code_rook.core.api import HttpApiServer, RuntimeApiService
from code_rook.core.api.auth import load_or_create_api_token
from code_rook.core.artifacts import ArtifactStore
from code_rook.core.artifacts.store import scan_referenced_artifact_shas
from code_rook.core.authority import RuntimeMode, ToolAction, WorkspaceTrust
from code_rook.core.authority.sandbox import detect_sandbox_capability
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    ArtifactGcCommand,
    ArtifactGcResult,
    ArtifactListCommand,
    ArtifactListResult,
    BackgroundCancelCommand,
    BackgroundCancelResult,
    BackgroundGetCommand,
    BackgroundGetResult,
    BackgroundJobInfo,
    CoreShutdownCommand,
    CoreShutdownResult,
    EventReplayCommand,
    EventReplayResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    GoalActionResult,
    GoalClearCommand,
    GoalCompleteCommand,
    GoalCreateCommand,
    GoalCreateResult,
    GoalEditCommand,
    GoalGetCommand,
    GoalGetResult,
    GoalListCommand,
    GoalListResult,
    GoalPauseCommand,
    GoalResumeCommand,
    HookAuditInfo,
    HookConfigInfo,
    HookRerunCommand,
    HookRerunResult,
    HooksListCommand,
    HooksListResult,
    McpListCommand,
    McpListResult,
    McpServerInfo,
    MemoryDeleteCommand,
    MemoryDeleteResult,
    MemoryInfo,
    MemoryListCommand,
    MemoryListResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    RunSteerCommand,
    RunSteerResult,
    RuntimeCapabilitiesCommand,
    RuntimeCapabilitiesResult,
    SessionAuthorityResult,
    SessionCheckpointsCommand,
    SessionCheckpointsResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionContextCommand,
    SessionContextResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionDeleteCommand,
    SessionDeleteResult,
    SessionExportCommand,
    SessionExportResult,
    SessionForkCommand,
    SessionForkResult,
    SessionGetAuthorityCommand,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionInfo,
    SessionListCommand,
    SessionListResult,
    SessionRenameCommand,
    SessionRenameResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionRewindCommand,
    SessionRewindResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSetAuthorityCommand,
    SessionTasksCommand,
    SessionTasksResult,
    ThreadArchiveCommand,
    ThreadArchiveResult,
    ThreadCreateCommand,
    ThreadCreateResult,
    ThreadGetCommand,
    ThreadGetResult,
    ThreadListCommand,
    ThreadListResult,
    ThreadUpdateCommand,
    ThreadUpdateResult,
    TurnGetCommand,
    TurnGetResult,
    TurnInspectCommand,
    TurnInspectResult,
    TurnInterruptCommand,
    TurnInterruptResult,
    TurnItemsCommand,
    TurnItemsResult,
    TurnListCommand,
    TurnListResult,
    TurnStartCommand,
    TurnStartResult,
    TurnSteerCommand,
    TurnSteerResult,
    UserQuestionRespondCommand,
    UserQuestionRespondResult,
    WorkerCancelCommand,
    WorkerCancelResult,
    WorkerListCommand,
    WorkerListResult,
    WorkflowGetCommand,
    WorkflowGetResult,
    WorkflowListCommand,
    WorkflowListResult,
    WorkflowStartCommand,
    WorkflowStartResult,
    WorkspaceDiffCommand,
    WorkspaceDiffResult,
)
from code_rook.core.bus.envelope import INVALID_PARAMS, EventPushEnvelope, HandlerError
from code_rook.core.bus.events import LlmUsageEvent
from code_rook.core.config import CodeRookConfig, get_config
from code_rook.core.daemon_lock import DaemonLock, DaemonLockError
from code_rook.core.events.bus import EventBus
from code_rook.core.fleet import (
    FleetProfile,
    LocalFleet,
    LocalFleetScheduler,
    LocalProcessHost,
    SQLiteWorkerStore,
)
from code_rook.core.goal import GoalService, GoalStore, GoalStoreError
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import HeadlessQuestionPolicy, InteractionManager
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.logging_setup import setup_logging
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.memory import MemoryStore
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.permissions.storage import load_policy_file
from code_rook.core.persistent_shell import PersistentShellPool
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.runner import AgentRunner
from code_rook.core.runs import events_file, new_run_id
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.sandbox.planner import SandboxTier, plan_sandbox
from code_rook.core.session import Session, SessionManager, SessionStore
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.writer import TraceWriter
from code_rook.core.transport.auth import load_or_create_ipc_token, require_loopback_host
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster
from code_rook.core.transport.socket_server import SocketServer, get_connection_writer
from code_rook.core.workflow import (
    WorkflowLedger,
    WorkflowLedgerError,
    WorkflowParseError,
    parse_workflow_text,
)
from code_rook.core.workspace import WorkspaceBoundary

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._daemon_lock: DaemonLock | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: CodeRookConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._goal_service: GoalService | None = None
        self._runtime: RuntimeService | None = None
        self._route_registry: RouteRegistry | None = None
        self._permission_manager: PermissionManager | None = None
        self._hooks: HookManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._process_supervisor = ProcessSupervisor()
        self._persistent_shell_pool = PersistentShellPool(
            process_supervisor=self._process_supervisor
        )
        self._background_registry = BackgroundJobRegistry(
            self._bus,
            self._process_supervisor,
        )
        self._interaction_manager = InteractionManager(self._bus)
        # daemon 级后台 subagent 任务注册表，跨 turn 持有
        self._subagent_registry: BackgroundTaskRegistry | None = None
        self._fleet_registry: BackgroundTaskRegistry | None = None
        self._fleet: LocalFleet | None = None
        self._http_api: HttpApiServer | None = None
        self._runtime_api: RuntimeApiService | None = None
        self._artifact_store = ArtifactStore(Path.cwd() / ".coderook" / "artifacts")

    # 收集 session 与工作区元数据文件，供 artifact GC 建立引用保留集合
    def _artifact_reference_paths(self) -> list[Path]:
        roots = [
            Path("~/.coderook/sessions").expanduser(),
            Path.cwd() / ".coderook",
        ]
        artifact_root = (Path.cwd() / ".coderook" / "artifacts").resolve()
        paths: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if path.resolve().is_relative_to(artifact_root):
                        continue
                except OSError:
                    continue
                paths.append(path)
        return paths

    # 返回 artifact 清单、总大小和按当前保留期可回收大小
    async def _artifact_list_handler(self, params: dict[str, Any]) -> ArtifactListResult:
        cmd = ArtifactListCommand.model_validate(params)
        keep = await asyncio.to_thread(
            scan_referenced_artifact_shas,
            self._artifact_reference_paths(),
        )
        inventory = await asyncio.to_thread(
            self._artifact_store.inventory,
            days=cmd.days,
            keep=keep,
        )
        return ArtifactListResult(
            artifacts=inventory,
            total_bytes=sum(item.size for item in inventory),
            reclaimable_bytes=sum(item.size for item in inventory if item.gc_candidate),
        )

    # 默认预览 GC；确认后重新扫描引用、删除候选并追加脱敏 receipt
    async def _artifact_gc_handler(self, params: dict[str, Any]) -> ArtifactGcResult:
        cmd = ArtifactGcCommand.model_validate(params)
        first = await self._artifact_list_handler({"days": cmd.days})
        candidates = [item.sha256 for item in first.artifacts if item.gc_candidate]
        if not cmd.confirmed:
            return ArtifactGcResult(
                dry_run=True,
                candidates=candidates,
                reclaimable_bytes=first.reclaimable_bytes,
            )

        keep = await asyncio.to_thread(
            scan_referenced_artifact_shas,
            self._artifact_reference_paths(),
        )
        second_inventory = await asyncio.to_thread(
            self._artifact_store.inventory,
            days=cmd.days,
            keep=keep,
        )
        candidate_sizes = {
            item.sha256: item.size for item in second_inventory if item.gc_candidate
        }
        removed_paths = await asyncio.to_thread(
            self._artifact_store.gc,
            days=cmd.days,
            keep=keep,
            dry_run=False,
        )
        removed = [path.name for path in removed_paths]
        reclaimed = sum(candidate_sizes.get(sha256, 0) for sha256 in removed)
        receipt_path = Path("~/.coderook/artifact-gc-receipts.jsonl").expanduser()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 1,
            "ts": _now(),
            "workspace": str(Path.cwd()),
            "days": cmd.days,
            "removed": removed,
            "reclaimed_bytes": reclaimed,
        }
        with receipt_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        return ArtifactGcResult(
            dry_run=False,
            candidates=[item.sha256 for item in second_inventory if item.gc_candidate],
            removed=removed,
            reclaimable_bytes=reclaimed,
            receipt_path=str(receipt_path),
        )

    # 从当前 route、agent profile 和 authority 真值构建不可变 FleetProfile
    def _build_fleet_profiles(self) -> list[FleetProfile]:
        assert self._route_registry is not None
        assert self._permission_manager is not None
        default_route = self._route_registry.route()
        default_authority = self._permission_manager.get_authority_snapshot("")
        profiles = {
            "default": FleetProfile(
                name="default",
                route=default_route.id,
                model=default_route.model,
                authority_ceiling=default_authority,
            )
        }
        for agent in AgentProfileLoader(Path.cwd()).list_all():
            route = (
                self._route_registry.route(agent.route)
                if agent.route
                else default_route
            )
            authority = default_authority
            if agent.restrict == "read_only":
                authority = authority.model_copy(
                    update={
                        "mode": RuntimeMode.PLAN,
                        "allowed_actions": frozenset({ToolAction.READ}),
                    }
                )
            profiles[agent.name] = FleetProfile(
                name=agent.name,
                route=route.id,
                model=agent.model or route.model,
                reasoning=agent.reasoning,
                authority_ceiling=authority,
            )
        return list(profiles.values())

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=code_rook.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 处理 core.shutdown 请求：触发有序关闭流程并立即返回确认
    async def _shutdown_handler(self, params: dict[str, Any]) -> CoreShutdownResult:
        CoreShutdownCommand.model_validate(params)
        logger.info("shutdown requested via IPC")
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        return CoreShutdownResult()

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        assert self._permission_manager is not None
        cmd = AgentRunCommand.model_validate(params)
        session = (
            await self._sessions.resume(cmd.resume_session_id)
            if cmd.resume_session_id is not None
            else await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        )
        self._permission_manager.set_session_mode(
            session.id,
            cmd.permission_mode,
            allow_tools=cmd.allow_tools,
        )
        self._interaction_manager.set_question_policy(
            session.id,
            HeadlessQuestionPolicy(
                mode=cmd.question_mode,
                timeout_s=cmd.question_timeout_s,
                answers=tuple(cmd.preset_answers),
            ),
        )
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)

        def _cleanup(completed: asyncio.Task[Any]) -> None:
            self._running_runs.discard(completed)
            if self._permission_manager is not None:
                self._permission_manager.clear_session_mode(session.id)
            self._interaction_manager.clear_question_policy(session.id)

        run_task.add_done_callback(_cleanup)
        return AgentRunResult(run_id=run_id, session_id=session.id)

    # 将模型 token 用量计入关联 Goal，并在预算耗尽后异步取消当前 run
    async def _goal_usage_event_handler(self, event: BaseModel) -> None:
        if not isinstance(event, LlmUsageEvent) or self._goal_service is None:
            return
        goal = next(
            (
                item
                for item in self._goal_service.list_all()
                if item.current_run_id == event.run_id
            ),
            None,
        )
        if goal is None:
            return
        tokens = (
            event.input_tokens
            + event.output_tokens
            + event.cache_read_input_tokens
            + event.cache_creation_input_tokens
        )
        updated = self._goal_service.record_usage(goal.id, tokens=tokens)
        if (
            goal.status == "active"
            and updated.status == "paused"
            and self._sessions is not None
        ):
            task = asyncio.create_task(self._sessions.cancel_run(event.run_id))
            self._running_runs.add(task)
            task.add_done_callback(self._running_runs.discard)
            task.add_done_callback(
                lambda completed: completed.exception()
                if not completed.cancelled()
                else None
            )

    # 创建持久 Goal，并按请求在所属 session 中立即开始首轮执行
    async def _goal_create_handler(self, params: dict[str, Any]) -> GoalCreateResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalCreateCommand.model_validate(params)
        self._sessions.get_session(cmd.session_id)
        if self._sessions.is_busy(cmd.session_id):
            raise HandlerError(INVALID_PARAMS, "cannot create goal during an active turn")
        try:
            goal = self._goal_service.create(
                cmd.objective,
                session_id=cmd.session_id,
                token_budget=cmd.token_budget,
                constraints=cmd.constraints,
                completion_criteria=cmd.completion_criteria,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        run_id: str | None = None
        if cmd.start:
            try:
                run_id = await self._sessions.send_message(cmd.session_id, goal.objective)
            except HandlerError:
                self._goal_service.set_status(goal.id, "blocked", actor="system")
                raise
            goal = self._goal_service.get(goal.id)
        return GoalCreateResult(goal=goal, run_id=run_id)

    # 查询 Goal ID 或 session 当前 Goal，不存在的 session Goal 返回空结果
    async def _goal_get_handler(self, params: dict[str, Any]) -> GoalGetResult:
        assert self._goal_service is not None
        cmd = GoalGetCommand.model_validate(params)
        try:
            if cmd.goal_id.strip():
                return GoalGetResult(goal=self._goal_service.get(cmd.goal_id.strip()))
            return GoalGetResult(goal=self._goal_service.current(cmd.session_id.strip()))
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 按 session、状态和数量稳定列出持久 Goal
    async def _goal_list_handler(self, params: dict[str, Any]) -> GoalListResult:
        assert self._goal_service is not None
        cmd = GoalListCommand.model_validate(params)
        goals = self._goal_service.list_all()
        if cmd.session_id.strip():
            goals = [goal for goal in goals if goal.session_id == cmd.session_id.strip()]
        if cmd.status is not None:
            goals = [goal for goal in goals if goal.status == cmd.status]
        return GoalListResult(goals=goals[-cmd.limit :])

    # 修改当前 Goal，并把新目标实时纠偏到正在执行的 run
    async def _goal_edit_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalEditCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            goal = self._goal_service.edit(
                selected.id,
                cmd.objective,
                completion_criteria=cmd.completion_criteria,
            )
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.steer_run(
                    run_id,
                    f"The active goal was edited. New objective: {goal.objective}",
                )
            return GoalActionResult(goal=goal, run_id=run_id)
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 暂停 Goal，并取消当前 run 以确保暂停立即生效
    async def _goal_pause_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalPauseCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            goal = self._goal_service.pause(selected.id)
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.cancel_run(run_id)
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 恢复暂停或阻塞 Goal，并在所属 session 中开始新的继续轮次
    async def _goal_resume_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalResumeCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            if self._sessions.is_busy(selected.session_id):
                raise ValueError("cannot resume goal during an active turn")
            goal = self._goal_service.resume(selected.id)
            try:
                run_id = await self._sessions.send_message(
                    goal.session_id,
                    "Continue working toward the active goal and verify every "
                    "completion criterion.",
                )
            except HandlerError:
                self._goal_service.set_status(goal.id, "blocked", actor="system")
                raise
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 清除 Goal，并取消仍在运行的关联 turn，同时保留审计文件
    async def _goal_clear_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalClearCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            goal = self._goal_service.clear(selected.id)
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.cancel_run(run_id)
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 由用户显式确认完成 Goal，并使用最近一次 run 或用户确认作为审计证据
    async def _goal_complete_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalCompleteCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            reference = (
                f"run://{selected.linked_run_ids[-1]}"
                if selected.linked_run_ids
                else "user://confirmation"
            )
            goal = self._goal_service.complete(
                selected.id,
                evidence=[("user-confirmation", reference)],
                summary=cmd.summary or "User confirmed the goal is complete.",
                actor="user",
            )
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.cancel_run(run_id)
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 取消指定 active run，并等待 Session 状态稳定落盘
    async def _run_cancel_handler(self, params: dict[str, Any]) -> RunCancelResult:
        assert self._sessions is not None
        cmd = RunCancelCommand.model_validate(params)
        session_id = await self._sessions.cancel_run(cmd.run_id)
        return RunCancelResult(run_id=cmd.run_id, session_id=session_id)

    # 将新用户指令排入活动 run 的纠偏队列
    async def _run_steer_handler(self, params: dict[str, Any]) -> RunSteerResult:
        assert self._sessions is not None
        cmd = RunSteerCommand.model_validate(params)
        await self._sessions.steer_run(cmd.run_id, cmd.content)
        return RunSteerResult(run_id=cmd.run_id)

    # 按 thread 事件游标分页返回已持久化 runtime 事件
    async def _event_replay_handler(self, params: dict[str, Any]) -> EventReplayResult:
        assert self._runtime is not None
        cmd = EventReplayCommand.model_validate(params)
        events = await self._runtime.list_events(
            cmd.thread_id,
            after_seq=cmd.after_seq,
            limit=cmd.limit,
        )
        latest_seq = await self._runtime.latest_event_seq(cmd.thread_id)
        last_seq = events[-1].seq if events else cmd.after_seq
        return EventReplayResult(
            events=events,
            latest_seq=latest_seq,
            has_more=last_seq < latest_seq,
        )

    # 创建兼容 session 并返回同一 durable thread
    async def _thread_create_handler(self, params: dict[str, Any]) -> ThreadCreateResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = ThreadCreateCommand.model_validate(params)
        session = await self._sessions.create(mode=cmd.mode, title=cmd.title)
        return ThreadCreateResult(thread=await self._runtime.get_thread(session.id))

    # 列出 durable threads 并按归档状态和数量裁剪
    async def _thread_list_handler(self, params: dict[str, Any]) -> ThreadListResult:
        assert self._runtime is not None
        cmd = ThreadListCommand.model_validate(params)
        threads = await self._runtime.list_threads()
        if not cmd.include_archived:
            threads = [thread for thread in threads if thread.status.value != "archived"]
        return ThreadListResult(threads=threads[: cmd.limit])

    # 查询单个 durable thread
    async def _thread_get_handler(self, params: dict[str, Any]) -> ThreadGetResult:
        assert self._runtime is not None
        cmd = ThreadGetCommand.model_validate(params)
        return ThreadGetResult(thread=await self._runtime.get_thread(cmd.thread_id))

    # 通过 session facade 更新标题并返回 durable thread 投影
    async def _thread_update_handler(self, params: dict[str, Any]) -> ThreadUpdateResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = ThreadUpdateCommand.model_validate(params)
        await self._sessions.rename(cmd.thread_id, cmd.title)
        return ThreadUpdateResult(thread=await self._runtime.get_thread(cmd.thread_id))

    # 通过 session facade 归档 thread 并返回持久终态
    async def _thread_archive_handler(self, params: dict[str, Any]) -> ThreadArchiveResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = ThreadArchiveCommand.model_validate(params)
        await self._sessions.close(cmd.thread_id)
        return ThreadArchiveResult(thread=await self._runtime.get_thread(cmd.thread_id))

    # 异步启动 turn 并立即返回稳定 turn id
    async def _turn_start_handler(self, params: dict[str, Any]) -> TurnStartResult:
        assert self._sessions is not None
        cmd = TurnStartCommand.model_validate(params)
        turn_id = new_run_id()
        task = asyncio.create_task(
            self._sessions.send_message(
                cmd.thread_id,
                cmd.content,
                run_id=turn_id,
                runtime_mode=cmd.runtime_mode,
            )
        )
        self._running_runs.add(task)
        task.add_done_callback(self._running_runs.discard)
        return TurnStartResult(turn_id=turn_id)

    # 查询单个 durable turn
    async def _turn_get_handler(self, params: dict[str, Any]) -> TurnGetResult:
        assert self._runtime is not None
        cmd = TurnGetCommand.model_validate(params)
        return TurnGetResult(turn=await self._runtime.get_turn(cmd.turn_id))

    # 列出 thread 的全部 durable turns
    async def _turn_list_handler(self, params: dict[str, Any]) -> TurnListResult:
        assert self._runtime is not None
        cmd = TurnListCommand.model_validate(params)
        return TurnListResult(turns=await self._runtime.list_turns(cmd.thread_id))

    # 中断活动 turn 并返回持久终态
    async def _turn_interrupt_handler(
        self,
        params: dict[str, Any],
    ) -> TurnInterruptResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = TurnInterruptCommand.model_validate(params)
        await self._sessions.cancel_run(cmd.turn_id)
        return TurnInterruptResult(turn=await self._runtime.get_turn(cmd.turn_id))

    # 向活动 turn 排入纠偏指令并返回当前持久状态
    async def _turn_steer_handler(self, params: dict[str, Any]) -> TurnSteerResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = TurnSteerCommand.model_validate(params)
        await self._sessions.steer_run(cmd.turn_id, cmd.content)
        return TurnSteerResult(turn=await self._runtime.get_turn(cmd.turn_id))

    # 返回 turn 的全部 durable items
    async def _turn_items_handler(self, params: dict[str, Any]) -> TurnItemsResult:
        assert self._runtime is not None
        cmd = TurnItemsCommand.model_validate(params)
        await self._runtime.get_turn(cmd.turn_id)
        return TurnItemsResult(items=await self._runtime.list_items(cmd.turn_id))

    # 返回客户端可协商的统一 runtime 能力
    async def _runtime_capabilities_handler(
        self,
        params: dict[str, Any],
    ) -> RuntimeCapabilitiesResult:
        RuntimeCapabilitiesCommand.model_validate(params)
        return RuntimeCapabilitiesResult(
            version=code_rook.__version__,
            runtime_modes=list(RuntimeMode),
            features=[
                "durable_threads",
                "durable_turns",
                "event_cursor_replay",
                "interrupt",
                "steer",
                "turn_receipts",
            ],
        )

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(mode=cmd.mode, title=cmd.title)
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(
            cmd.session_id,
            cmd.content,
            runtime_mode=cmd.runtime_mode,
            attachments=cmd.attachments,
        )
        return SessionSendMessageResult(run_id=run_id)

    # 返回指定会话从下一轮开始使用的权限快照
    async def _session_get_authority_handler(
        self,
        params: dict[str, Any],
    ) -> SessionAuthorityResult:
        assert self._sessions is not None
        assert self._permission_manager is not None
        cmd = SessionGetAuthorityCommand.model_validate(params)
        self._sessions.get_session(cmd.session_id)
        return SessionAuthorityResult(
            snapshot=self._permission_manager.get_authority_snapshot(cmd.session_id)
        )

    # 独立更新后续轮次的 mode、profile 或 trust，并保留未指定维度
    async def _session_set_authority_handler(
        self,
        params: dict[str, Any],
    ) -> SessionAuthorityResult:
        assert self._sessions is not None
        assert self._permission_manager is not None
        cmd = SessionSetAuthorityCommand.model_validate(params)
        self._sessions.get_session(cmd.session_id)
        if self._sessions.is_busy(cmd.session_id):
            raise HandlerError(INVALID_PARAMS, "authority cannot change during an active turn")
        current = self._permission_manager.get_authority_snapshot(cmd.session_id)
        changes: dict[str, Any] = {}
        if cmd.mode is not None:
            changes["mode"] = cmd.mode
        if cmd.profile is not None:
            changes["profile"] = cmd.profile
        if cmd.workspace_trust is not None:
            changes["workspace_trust"] = cmd.workspace_trust
        updated = current.model_copy(update=changes)
        self._permission_manager.set_authority_snapshot(cmd.session_id, updated)
        if cmd.profile is not None:
            self._permission_manager.set_default_profile(cmd.profile)
        return SessionAuthorityResult(snapshot=updated)

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    @staticmethod
    def _session_info(session: Session) -> SessionInfo:
        return SessionInfo(
            session_id=session.id,
            mode=session.mode,
            status=session.status,
            title=session.title,
            created_at=session.created_at,
            updated_at=session.updated_at,
            run_count=len(session.run_ids),
            last_run_id=session.run_ids[-1] if session.run_ids else None,
            parent_session_id=session.parent_session_id,
        )

    # 列出 daemon 已恢复的持久化 sessions
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        cmd = SessionListCommand.model_validate(params)
        sessions = await self._sessions.list_sessions(
            include_closed=cmd.include_closed,
            limit=cmd.limit,
        )
        return SessionListResult(sessions=[self._session_info(session) for session in sessions])

    # 重新打开历史 chat session，后续消息会沿用其 thread
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        session = await self._sessions.resume(cmd.session_id)
        return SessionResumeResult(session=self._session_info(session))

    async def _session_rename_handler(self, params: dict[str, Any]) -> SessionRenameResult:
        assert self._sessions is not None
        cmd = SessionRenameCommand.model_validate(params)
        session = await self._sessions.rename(cmd.session_id, cmd.title)
        return SessionRenameResult(session=self._session_info(session))

    async def _session_fork_handler(self, params: dict[str, Any]) -> SessionForkResult:
        assert self._sessions is not None
        cmd = SessionForkCommand.model_validate(params)
        session = await self._sessions.fork(cmd.session_id, cmd.title)
        return SessionForkResult(session=self._session_info(session))

    async def _session_export_handler(self, params: dict[str, Any]) -> SessionExportResult:
        assert self._sessions is not None
        cmd = SessionExportCommand.model_validate(params)
        filename, media_type, content = await self._sessions.export(
            cmd.session_id,
            cmd.format,
        )
        return SessionExportResult(
            filename=filename,
            media_type=media_type,
            content=content,
        )

    async def _session_delete_handler(self, params: dict[str, Any]) -> SessionDeleteResult:
        assert self._sessions is not None
        cmd = SessionDeleteCommand.model_validate(params)
        await self._sessions.delete(cmd.session_id)
        return SessionDeleteResult(session_id=cmd.session_id)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.respond(
            cmd.tool_use_id,
            cmd.decision,
            selected_hunks=cmd.selected_hunks,
            patch_plan_id=cmd.patch_plan_id,
        )
        return PermissionRespondResult()

    # 接收用户对结构化问题的回答并恢复挂起的工具调用
    async def _user_question_respond_handler(
        self,
        params: dict[str, Any],
    ) -> UserQuestionRespondResult:
        cmd = UserQuestionRespondCommand.model_validate(params)
        if not self._interaction_manager.answer(cmd.question_id, cmd.answer):
            raise HandlerError(INVALID_PARAMS, "question is not pending")
        return UserQuestionRespondResult()

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 返回当前会话最近一次 run 的任务列表
    async def _session_tasks_handler(self, params: dict[str, Any]) -> SessionTasksResult:
        assert self._sessions is not None
        cmd = SessionTasksCommand.model_validate(params)
        run_id, tasks = self._sessions.list_tasks(cmd.session_id)
        return SessionTasksResult(run_id=run_id, tasks=tasks)

    # 返回持久 Worker 列表，省略 prompt 和完整子运行 transcript
    async def _worker_list_handler(self, params: dict[str, Any]) -> WorkerListResult:
        assert self._subagent_registry is not None
        cmd = WorkerListCommand.model_validate(params)
        workers = self._subagent_registry.list_records()
        if self._fleet_registry is not None:
            workers.extend(self._fleet_registry.list_records())
        if cmd.worker_id:
            workers = [item for item in workers if item.id == cmd.worker_id]
        if cmd.root_goal_id:
            workers = [
                item for item in workers if item.root_goal_id == cmd.root_goal_id
            ]
        if cmd.session_id:
            workers = [item for item in workers if item.session_id == cmd.session_id]
        payload = [
            {
                "worker_id": item.id,
                "parent_turn_id": item.parent_turn_id,
                "root_goal_id": item.root_goal_id,
                "description": item.description,
                "role": item.role,
                "profile": item.profile,
                "route": item.route,
                "model": item.model,
                "reasoning": item.reasoning,
                "status": item.status.value,
                "status_reason": item.status_reason,
                "depth": item.depth,
                "attempt": item.attempt,
                "max_attempts": item.max_attempts,
                "worktree": item.worktree,
                "token_budget": item.token_budget,
                "token_usage": item.token_usage,
                "heartbeat_at": item.heartbeat_at,
                "summary": item.summary[:1_000],
                "blockers": item.blockers[:10],
            }
            for item in workers[-cmd.limit :]
        ]
        return WorkerListResult(workers=payload)

    # 取消持久 Worker 并返回其新状态（先查内存子代理再查 fleet）
    async def _worker_cancel_handler(self, params: dict[str, Any]) -> WorkerCancelResult:
        cmd = WorkerCancelCommand.model_validate(params)
        assert self._subagent_registry is not None
        if self._subagent_registry.record(cmd.worker_id) is not None:
            worker = await self._subagent_registry.cancel(cmd.worker_id)
            return WorkerCancelResult(
                worker_id=cmd.worker_id, status=worker.status.value
            )
        if (
            self._fleet_registry is not None
            and self._fleet_registry.record(cmd.worker_id) is not None
        ):
            worker = await self._fleet_registry.cancel(cmd.worker_id)
            return WorkerCancelResult(
                worker_id=cmd.worker_id, status=worker.status.value
            )
        raise HandlerError(INVALID_PARAMS, f"worker not found: {cmd.worker_id}")

    # 返回后台 shell 任务列表，或单个任务的全量增量输出
    async def _background_get_handler(self, params: dict[str, Any]) -> BackgroundGetResult:
        cmd = BackgroundGetCommand.model_validate(params)
        records = (
            [self._background_registry.get(cmd.job_id)]
            if cmd.job_id
            else self._background_registry.list()
        )
        jobs = [
            BackgroundJobInfo(
                id=job.id,
                command=job.command,
                session_id=job.session_id,
                run_id=job.run_id,
                status=job.status,
                output=job.output,
                is_error=job.is_error,
                created_at=job.created_at,
                finished_at=job.finished_at,
                process_usage=job.process_usage or {},
            )
            for job in records
            if job is not None
        ]
        return BackgroundGetResult(jobs=jobs)

    # 取消指定后台 shell 任务并返回是否真正终止了运行
    async def _background_cancel_handler(
        self, params: dict[str, Any]
    ) -> BackgroundCancelResult:
        cmd = BackgroundCancelCommand.model_validate(params)
        cancelled = await self._background_registry.cancel(cmd.job_id)
        return BackgroundCancelResult(job_id=cmd.job_id, cancelled=cancelled)

    # 返回 MCP server 状态与工具清单
    async def _mcp_list_handler(self, params: dict[str, Any]) -> McpListResult:
        McpListCommand.model_validate(params)
        assert self._mcp_manager is not None
        servers = [
            McpServerInfo(
                name=str(state.get("name", "")),
                transport=str(state.get("transport", "")),
                status=str(state.get("status", "")),
                tool_count=len(state.get("tools", [])),
                tools=list(state.get("tools", [])),
                error=str(state.get("error", "")),
            )
            for state in self._mcp_manager.describe()
        ]
        return McpListResult(servers=servers)

    # 返回 hook 配置表与最近执行记录
    async def _hooks_list_handler(self, params: dict[str, Any]) -> HooksListResult:
        cmd = HooksListCommand.model_validate(params)
        assert self._hooks is not None
        configs = [
            HookConfigInfo(
                id=cfg.id,
                event=cfg.event,
                blocking=cfg.blocking,
                trusted_scope=cfg.trusted_scope,
                on_failure=cfg.on_failure,
                command=list(cfg.command),
                conditions=dict(cfg.conditions),
            )
            for cfg in self._hooks.configs
        ]
        audit_events = [
            HookAuditInfo(
                hook_id=item.hook_id,
                run_id=item.run_id,
                event=item.event,
                status=item.status,
                blocking=item.blocking,
                elapsed_ms=item.elapsed_ms,
                blocked=item.blocked,
                reason=item.reason,
                exit_code=item.exit_code,
                process_usage=item.process_usage,
                ts=item.ts,
            )
            for item in self._hooks.audit_events()[-cmd.limit :]
        ]
        return HooksListResult(configs=configs, audit_events=audit_events)

    # 手动重跑指定 hook，返回本次执行状态
    async def _hook_rerun_handler(self, params: dict[str, Any]) -> HookRerunResult:
        cmd = HookRerunCommand.model_validate(params)
        assert self._hooks is not None
        audit = await self._hooks.rerun(cmd.hook_id)
        if audit is None:
            raise HandlerError(INVALID_PARAMS, f"hook not found: {cmd.hook_id}")
        return HookRerunResult(
            hook_id=cmd.hook_id,
            executed=True,
            status=audit.status,
            reason=audit.reason,
            ts=audit.ts,
        )

    # 返回当前项目记忆条目列表
    async def _memory_list_handler(self, params: dict[str, Any]) -> MemoryListResult:
        MemoryListCommand.model_validate(params)
        store = MemoryStore(Path.cwd() / ".coderook" / "memory")
        return MemoryListResult(
            memories=[
                MemoryInfo(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    type=item.type,
                    body=item.body[:1_000],
                    source_session_id=item.source_session_id,
                    source_run_id=item.source_run_id,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in store.list_all()
            ]
        )

    # 删除指定记忆并返回是否删除成功
    async def _memory_delete_handler(self, params: dict[str, Any]) -> MemoryDeleteResult:
        cmd = MemoryDeleteCommand.model_validate(params)
        store = MemoryStore(Path.cwd() / ".coderook" / "memory")
        deleted = store.forget(cmd.memory_id)
        return MemoryDeleteResult(memory_id=cmd.memory_id, deleted=deleted)

    # 启动声明式 TOML/JSON workflow，并立即返回 durable workflow ID
    async def _workflow_start_handler(
        self,
        params: dict[str, Any],
    ) -> WorkflowStartResult:
        assert self._fleet is not None
        cmd = WorkflowStartCommand.model_validate(params)
        try:
            spec = parse_workflow_text(cmd.source, format=cmd.format)
            self._fleet.start(spec)
        except (WorkflowParseError, WorkflowLedgerError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkflowStartResult(workflow_id=spec.id)

    # 列出 durable workflow 元数据供 TUI/CLI projection 使用
    async def _workflow_list_handler(
        self,
        params: dict[str, Any],
    ) -> WorkflowListResult:
        assert self._fleet is not None
        cmd = WorkflowListCommand.model_validate(params)
        return WorkflowListResult(workflows=self._fleet.list()[-cmd.limit :])

    # 从 SQLite event reducer 返回单个 workflow 的完整 Work Graph
    async def _workflow_get_handler(
        self,
        params: dict[str, Any],
    ) -> WorkflowGetResult:
        assert self._fleet is not None
        cmd = WorkflowGetCommand.model_validate(params)
        try:
            graph = self._fleet.graph(cmd.workflow_id)
        except WorkflowLedgerError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkflowGetResult(workflow=graph.model_dump(mode="json"))

    # 返回工作区结构化 Git diff，供 TUI 直接展示文件和补丁
    async def _workspace_diff_handler(self, params: dict[str, Any]) -> WorkspaceDiffResult:
        cmd = WorkspaceDiffCommand.model_validate(params)
        result = await GitDiffTool(WorkspaceBoundary.current()).invoke(
            {"scope": cmd.scope, "path": cmd.path}
        )
        payload = json.loads(result.content)
        return WorkspaceDiffResult(payload=payload)

    # 返回当前会话指定（默认最近一次）run 的安全恢复点列表
    async def _session_checkpoints_handler(
        self,
        params: dict[str, Any],
    ) -> SessionCheckpointsResult:
        assert self._sessions is not None
        cmd = SessionCheckpointsCommand.model_validate(params)
        run_id, checkpoints = self._sessions.list_checkpoints(cmd.session_id, cmd.run_id)
        return SessionCheckpointsResult(run_id=run_id, checkpoints=checkpoints)

    # 恢复用户明确选择的 checkpoint 并返回受影响文件
    async def _session_rewind_handler(
        self,
        params: dict[str, Any],
    ) -> SessionRewindResult:
        assert self._sessions is not None
        cmd = SessionRewindCommand.model_validate(params)
        result = self._sessions.rewind(cmd.session_id, cmd.checkpoint_id, cmd.run_id)
        return SessionRewindResult.model_validate(result)

    # 返回当前会话的上下文大小和运行概览
    async def _session_context_handler(
        self,
        params: dict[str, Any],
    ) -> SessionContextResult:
        assert self._sessions is not None
        cmd = SessionContextCommand.model_validate(params)
        context = self._sessions.context_info(cmd.session_id)
        last_run_id = context.get("last_run_id")
        if isinstance(last_run_id, str) and self._runtime is not None:
            turn = await self._runtime.get_turn(last_run_id)
            events = [
                event
                for event in await self._runtime.list_events(cmd.session_id)
                if event.turn_id == last_run_id
            ]
            context["usage"] = turn.usage
            for event in reversed(events):
                if event.type == "context.working_set" and not context.get("working_set"):
                    paths = event.payload.get("paths")
                    context["working_set"] = paths if isinstance(paths, list) else []
                elif event.type == "context.compacted" and context.get("compaction") is None:
                    context["compaction"] = event.payload
                elif event.type == "context.budget" and context.get("tool_schema_tokens") is None:
                    context["tool_schema_tokens"] = event.payload.get("tool_schema_tokens")
                    context["system_tokens"] = event.payload.get("system_tokens")
        return SessionContextResult.model_validate(context)

    # 返回 turn inspector 所需的 durable 状态、items、events 与 receipt
    async def _turn_inspect_handler(self, params: dict[str, Any]) -> TurnInspectResult:
        assert self._runtime is not None
        cmd = TurnInspectCommand.model_validate(params)
        turn = await self._runtime.get_turn(cmd.turn_id)
        items = await self._runtime.list_items(cmd.turn_id)
        events = await self._runtime.list_turn_events(cmd.turn_id)
        receipt = await self._runtime.get_receipt(cmd.turn_id)
        return TurnInspectResult(
            turn=turn.model_dump(mode="json"),
            items=[item.model_dump(mode="json") for item in items],
            events=[event.model_dump(mode="json") for event in events],
            receipt=receipt.model_dump(mode="json"),
        )

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册事件订阅，并为 runtime thread 建立无缝历史回放与实时衔接
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()
        assert self._broadcaster is not None

        if cmd.thread_id is not None:
            return await self._subscribe_runtime_events(cmd, writer)
        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 先注册回放态订阅，再按高水位回放并刷新期间积压的实时事件
    async def _subscribe_runtime_events(
        self,
        cmd: EventSubscribeCommand,
        writer: asyncio.StreamWriter,
    ) -> EventSubscribeResult:
        assert self._runtime is not None
        assert self._broadcaster is not None
        assert cmd.thread_id is not None
        sub_id = self._broadcaster.subscribe(
            writer,
            cmd.topics,
            f"thread:{cmd.thread_id}",
            replaying_runtime=True,
        )
        replayed_count = 0
        cursor = cmd.after_seq
        try:
            high_water = await self._runtime.latest_event_seq(cmd.thread_id)
            while cursor < high_water:
                events = await self._runtime.list_events(
                    cmd.thread_id,
                    after_seq=cursor,
                    up_to_seq=high_water,
                    limit=1000,
                )
                if not events:
                    break
                replayed_count += await self._broadcaster.replay_runtime_batch(
                    sub_id,
                    events,
                )
                cursor = events[-1].seq
            pending_count, cursor = await self._broadcaster.finish_runtime_replay(
                sub_id,
                max(cursor, high_water),
            )
            replayed_count += pending_count
        except Exception:
            self._broadcaster.unsubscribe(writer)
            raise
        return EventSubscribeResult(
            subscription_id=sub_id,
            replayed_count=replayed_count,
            last_seq=cursor,
        )

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            for candidate in Path("~/.coderook/sessions").expanduser().glob(
                f"*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        require_loopback_host(self._config.host)
        setup_logging(self._config)

        self._daemon_lock = DaemonLock(Path("~/.coderook/core.lock").expanduser())
        try:
            self._daemon_lock.acquire()
        except DaemonLockError as exc:
            raise SystemExit(str(exc)) from None

        ipc_token = load_or_create_ipc_token(
            Path(self._config.ipc_token_file).expanduser()
        )

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(
                trace_path,
                max_bytes=self._config.trace.max_bytes,
                backup_count=self._config.trace.backup_count,
                include_payload=self._config.trace.include_payload,
            )
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = Path("~/.coderook/policy.toml").expanduser()
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )
        self._hooks = HookManager.from_workspace(
            Path.cwd(),
            bus=self._bus,
            project_trust_provider=lambda session_id: (
                self._permission_manager is not None
                and self._permission_manager.get_authority_snapshot(session_id).workspace_trust
                == WorkspaceTrust.TRUSTED
            ),
            process_supervisor=self._process_supervisor,
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.coderook/sessions").expanduser()
        store = SessionStore(sessions_root)
        self._goal_service = GoalService(GoalStore(sessions_root.parent / "goals"))
        recovered_goals = self._goal_service.recover_interrupted()
        if recovered_goals:
            logger.info("recovered %d interrupted goals", len(recovered_goals))
        self._bus.subscribe(self._goal_usage_event_handler)
        self._runtime = RuntimeService(
            RuntimeStore(sessions_root.parent / "runtime.db"),
            workspace=Path.cwd(),
            bus=self._bus,
            authority_provider=self._permission_manager.get_authority_snapshot,
        )
        self._bus.subscribe(self._runtime.record_bus_event)
        await self._runtime.recover_stale_turns(datetime.datetime.now(UTC))
        assert self._config is not None
        self._route_registry = RouteRegistry(self._config.llm)

        self._mcp_manager = McpServerManager(self._process_supervisor)
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        # daemon 级 Worker 控制面跨 turn 和重启持久化，内存仅保留本次 boot 任务句柄
        self._subagent_registry = BackgroundTaskRegistry(
            store_path=sessions_root.parent / "workers"
        )
        self._fleet_registry = BackgroundTaskRegistry(
            store=SQLiteWorkerStore(sessions_root.parent / "fleet.db")
        )
        fleet_scheduler = LocalFleetScheduler(
            self._fleet_registry,
            LocalProcessHost(
                (sys.executable, "-m", "code_rook.core.fleet.worker_process"),
                cwd=Path.cwd(),
                process_supervisor=self._process_supervisor,
                sandbox_plan=plan_sandbox(
                    detect_sandbox_capability(),
                    SandboxTier.WORKSPACE_WRITE,
                    str(Path.cwd()),
                    network=True,
                ),
            ),
            workspace=Path.cwd(),
            profiles=self._build_fleet_profiles(),
        )
        self._fleet = LocalFleet(
            WorkflowLedger(sessions_root.parent / "workflow.db"),
            fleet_scheduler,
        )
        resumed_workflows = self._fleet.resume_all()
        if resumed_workflows:
            logger.info("resumed %d durable workflows", len(resumed_workflows))

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                background_registry=self._background_registry,
                subagent_registry=self._subagent_registry,
                interaction_manager=self._interaction_manager,
                route_registry=self._route_registry,
                runtime_service=self._runtime,
                hooks=self._hooks,
                process_supervisor=self._process_supervisor,
                persistent_shell_pool=self._persistent_shell_pool,
                goal_service=self._goal_service,
            ),
            bus=self._bus,
            subagent_registry=self._subagent_registry,
            runtime_service=self._runtime,
            interaction_manager=self._interaction_manager,
            route_registry=self._route_registry,
            hooks=self._hooks,
            goal_service=self._goal_service,
        )
        self._runtime_api = RuntimeApiService(
            self._runtime,
            self._sessions,
            permission_manager=self._permission_manager,
            workspace_boundary=WorkspaceBoundary.current(),
        )
        self._bus.subscribe(self._runtime_api.notify_runtime_event)
        if not self._config.api.token:
            self._config.api.token = load_or_create_api_token(
                Path("~/.coderook/api-token").expanduser()
            )
        self._http_api = HttpApiServer(
            self._config.api.host,
            self._config.api.port,
            self._config.api.token,
            self._runtime_api,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
            auth_token=ipc_token,
        )
        server.register("core.ping", self._ping_handler)
        server.register("core.shutdown", self._shutdown_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("goal.create", self._goal_create_handler)
        server.register("goal.get", self._goal_get_handler)
        server.register("goal.list", self._goal_list_handler)
        server.register("goal.edit", self._goal_edit_handler)
        server.register("goal.pause", self._goal_pause_handler)
        server.register("goal.resume", self._goal_resume_handler)
        server.register("goal.clear", self._goal_clear_handler)
        server.register("goal.complete", self._goal_complete_handler)
        server.register("run.cancel", self._run_cancel_handler)
        server.register("run.steer", self._run_steer_handler)
        server.register("artifact.list", self._artifact_list_handler)
        server.register("artifact.gc", self._artifact_gc_handler)
        server.register("event.replay", self._event_replay_handler)
        server.register("thread.create", self._thread_create_handler)
        server.register("thread.list", self._thread_list_handler)
        server.register("thread.get", self._thread_get_handler)
        server.register("thread.update", self._thread_update_handler)
        server.register("thread.archive", self._thread_archive_handler)
        server.register("turn.start", self._turn_start_handler)
        server.register("turn.get", self._turn_get_handler)
        server.register("turn.list", self._turn_list_handler)
        server.register("turn.interrupt", self._turn_interrupt_handler)
        server.register("turn.steer", self._turn_steer_handler)
        server.register("turn.items", self._turn_items_handler)
        server.register("runtime.capabilities", self._runtime_capabilities_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_authority", self._session_get_authority_handler)
        server.register("session.set_authority", self._session_set_authority_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.rename", self._session_rename_handler)
        server.register("session.fork", self._session_fork_handler)
        server.register("session.export", self._session_export_handler)
        server.register("session.delete", self._session_delete_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("user_question.respond", self._user_question_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("session.tasks", self._session_tasks_handler)
        server.register("worker.list", self._worker_list_handler)
        server.register("workflow.start", self._workflow_start_handler)
        server.register("workflow.list", self._workflow_list_handler)
        server.register("workflow.get", self._workflow_get_handler)
        server.register("workspace.diff", self._workspace_diff_handler)
        server.register("session.checkpoints", self._session_checkpoints_handler)
        server.register("session.rewind", self._session_rewind_handler)
        server.register("session.context", self._session_context_handler)
        server.register("turn.inspect", self._turn_inspect_handler)
        server.register("mcp.list", self._mcp_list_handler)
        server.register("hooks.list", self._hooks_list_handler)
        server.register("hooks.rerun", self._hook_rerun_handler)
        server.register("memory.list", self._memory_list_handler)
        server.register("memory.delete", self._memory_delete_handler)
        server.register("background.get", self._background_get_handler)
        server.register("background.cancel", self._background_cancel_handler)
        server.register("worker.cancel", self._worker_cancel_handler)

        addr = await server.start()
        try:
            api_addr = await self._http_api.start()
        except Exception:
            await server.stop()
            raise
        logger.info("coderook-core %s listening addr=%s", code_rook.__version__, addr)
        logger.info("runtime API listening addr=%s", api_addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        self._shutdown_event = shutdown
        try:
            loop.add_signal_handler(signal.SIGINT, shutdown.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handlers are not supported by this event loop")

        await shutdown.wait()

        logger.info("shutting down")
        if self._http_api is not None:
            await self._http_api.stop()
        if self._fleet is not None:
            await self._fleet.shutdown()
        if self._subagent_registry is not None:
            self._subagent_registry.begin_shutdown()
        if self._sessions is not None:
            await self._sessions.cancel_all()
        if self._runtime_api is not None:
            await self._runtime_api.close()
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        if self._runtime is not None:
            await self._runtime.drain_pending_writes()
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await self._background_registry.cancel_all()
        if self._subagent_registry is not None:
            await self._subagent_registry.cancel_all()
        if self._hooks is not None:
            await self._hooks.close()
        await self._persistent_shell_pool.aclose_all()
        await self._process_supervisor.close()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()
        if self._daemon_lock is not None:
            self._daemon_lock.release()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    migrate_legacy_state()
    asyncio.run(CoreApp().run())

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import signal
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import code_rook
from code_rook.core.authority import WorkspaceTrust
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventReplayCommand,
    EventReplayResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    RunSteerCommand,
    RunSteerResult,
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
    UserQuestionRespondCommand,
    UserQuestionRespondResult,
    WorkerListCommand,
    WorkerListResult,
    WorkspaceDiffCommand,
    WorkspaceDiffResult,
)
from code_rook.core.bus.envelope import INVALID_PARAMS, EventPushEnvelope, HandlerError
from code_rook.core.config import CodeRookConfig, get_config
from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.logging_setup import setup_logging
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.permissions.storage import load_policy_file
from code_rook.core.runner import AgentRunner
from code_rook.core.runs import events_file, new_run_id
from code_rook.core.runtime import RuntimeService, RuntimeStore
from code_rook.core.session import Session, SessionManager, SessionStore
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.writer import TraceWriter
from code_rook.core.transport.auth import load_or_create_ipc_token, require_loopback_host
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster
from code_rook.core.transport.socket_server import SocketServer, get_connection_writer
from code_rook.core.workspace import WorkspaceBoundary

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: CodeRookConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._runtime: RuntimeService | None = None
        self._route_registry: RouteRegistry | None = None
        self._permission_manager: PermissionManager | None = None
        self._hooks: HookManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._background_registry = BackgroundJobRegistry(self._bus)
        self._interaction_manager = InteractionManager(self._bus)
        # daemon 级后台 subagent 任务注册表，跨 turn 持有
        self._subagent_registry: BackgroundTaskRegistry | None = None

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=code_rook.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

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
        session = await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        self._permission_manager.set_session_mode(
            session.id,
            cmd.permission_mode,
            allow_tools=cmd.allow_tools,
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

        run_task.add_done_callback(_cleanup)
        return AgentRunResult(run_id=run_id)

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
        self._permission_manager.respond(cmd.tool_use_id, cmd.decision)
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

    # 返回工作区结构化 Git diff，供 TUI 直接展示文件和补丁
    async def _workspace_diff_handler(self, params: dict[str, Any]) -> WorkspaceDiffResult:
        cmd = WorkspaceDiffCommand.model_validate(params)
        result = await GitDiffTool(WorkspaceBoundary.current()).invoke(
            {"scope": cmd.scope, "path": cmd.path}
        )
        payload = json.loads(result.content)
        return WorkspaceDiffResult(payload=payload)

    # 返回当前会话最近一次 run 的安全恢复点列表
    async def _session_checkpoints_handler(
        self,
        params: dict[str, Any],
    ) -> SessionCheckpointsResult:
        assert self._sessions is not None
        cmd = SessionCheckpointsCommand.model_validate(params)
        run_id, checkpoints = self._sessions.list_checkpoints(cmd.session_id)
        return SessionCheckpointsResult(run_id=run_id, checkpoints=checkpoints)

    # 恢复用户明确选择的 checkpoint 并返回受影响文件
    async def _session_rewind_handler(
        self,
        params: dict[str, Any],
    ) -> SessionRewindResult:
        assert self._sessions is not None
        cmd = SessionRewindCommand.model_validate(params)
        result = self._sessions.rewind(cmd.session_id, cmd.checkpoint_id)
        return SessionRewindResult.model_validate(result)

    # 返回当前会话的上下文大小和运行概览
    async def _session_context_handler(
        self,
        params: dict[str, Any],
    ) -> SessionContextResult:
        assert self._sessions is not None
        cmd = SessionContextCommand.model_validate(params)
        return SessionContextResult.model_validate(
            self._sessions.context_info(cmd.session_id)
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
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.coderook/sessions").expanduser()
        store = SessionStore(sessions_root)
        self._runtime = RuntimeService(
            RuntimeStore(sessions_root.parent / "runtime.db"),
            workspace=Path.cwd(),
            bus=self._bus,
            authority_provider=self._permission_manager.get_authority_snapshot,
        )
        await self._runtime.recover_stale_turns(datetime.datetime.now(UTC))
        assert self._config is not None
        self._route_registry = RouteRegistry(self._config.llm)

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        # daemon 级 Worker 控制面跨 turn 和重启持久化，内存仅保留本次 boot 任务句柄
        self._subagent_registry = BackgroundTaskRegistry(
            store_path=sessions_root.parent / "workers"
        )

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
            ),
            bus=self._bus,
            subagent_registry=self._subagent_registry,
            runtime_service=self._runtime,
            interaction_manager=self._interaction_manager,
            route_registry=self._route_registry,
            hooks=self._hooks,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
            auth_token=ipc_token,
        )
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("run.cancel", self._run_cancel_handler)
        server.register("run.steer", self._run_steer_handler)
        server.register("event.replay", self._event_replay_handler)
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
        server.register("workspace.diff", self._workspace_diff_handler)
        server.register("session.checkpoints", self._session_checkpoints_handler)
        server.register("session.rewind", self._session_rewind_handler)
        server.register("session.context", self._session_context_handler)

        addr = await server.start()
        logger.info("coderook-core %s listening addr=%s", code_rook.__version__, addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        try:
            loop.add_signal_handler(signal.SIGINT, shutdown.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handlers are not supported by this event loop")

        await shutdown.wait()

        logger.info("shutting down")
        if self._subagent_registry is not None:
            self._subagent_registry.begin_shutdown()
        if self._sessions is not None:
            await self._sessions.cancel_all()
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await self._background_registry.cancel_all()
        if self._subagent_registry is not None:
            await self._subagent_registry.cancel_all()
        if self._hooks is not None:
            await self._hooks.close()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    migrate_legacy_state()
    asyncio.run(CoreApp().run())

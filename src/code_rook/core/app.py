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
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionDeleteCommand,
    SessionDeleteResult,
    SessionExportCommand,
    SessionExportResult,
    SessionForkCommand,
    SessionForkResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionInfo,
    SessionListCommand,
    SessionListResult,
    SessionRenameCommand,
    SessionRenameResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
)
from code_rook.core.bus.envelope import EventPushEnvelope
from code_rook.core.config import CodeRookConfig, get_config
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.factory import create_llm_provider
from code_rook.core.logging_setup import setup_logging
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.permissions.storage import load_policy_file
from code_rook.core.runner import AgentRunner
from code_rook.core.runs import events_file, new_run_id
from code_rook.core.session import Session, SessionManager, SessionStore
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.writer import TraceWriter
from code_rook.core.transport.auth import load_or_create_ipc_token, require_loopback_host
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster
from code_rook.core.transport.socket_server import SocketServer, get_connection_writer

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
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._background_registry = BackgroundJobRegistry(self._bus)
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
        run_id = await self._sessions.send_message(cmd.session_id, cmd.content)
        return SessionSendMessageResult(run_id=run_id)

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

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放 events.jsonl 历史再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

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

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = Path("~/.coderook/sessions").expanduser()
        store = SessionStore(sessions_root)
        assert self._config is not None
        compact_provider = create_llm_provider(self._config.llm)

        self._mcp_manager = McpServerManager()
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        # daemon 级后台 subagent 注册表，跨 turn 持有，session/turn 间不销毁
        self._subagent_registry = BackgroundTaskRegistry()

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
            ),
            bus=self._bus,
            provider=compact_provider,
            subagent_registry=self._subagent_registry,
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
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.rename", self._session_rename_handler)
        server.register("session.fork", self._session_fork_handler)
        server.register("session.export", self._session_export_handler)
        server.register("session.delete", self._session_delete_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)

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
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    migrate_legacy_state()
    asyncio.run(CoreApp().run())

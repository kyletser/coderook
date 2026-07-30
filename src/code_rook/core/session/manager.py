from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.envelope import INVALID_PARAMS, HandlerError
from code_rook.core.bus.events import (
    PlanReadyEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionDeletedEvent,
    SessionForkedEvent,
    SessionInterruptedEvent,
    SessionMessageReceivedEvent,
    SessionRenamedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.runs import new_run_id
from code_rook.core.runtime.models import TurnStatus
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.session.exporter import SessionExportFormat, export_session
from code_rook.core.session.model import Session, SessionMode
from code_rook.core.session.store import SessionStore
from code_rook.core.skills.loader import SkillLoader

if TYPE_CHECKING:
    from code_rook.core.llm.base import LLMProvider
    from code_rook.core.runner import AgentRunner
    from code_rook.core.subagent.registry import BackgroundTaskRegistry

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_NOT_RESUMABLE = -32013
RUN_NOT_ACTIVE = -32014


@dataclass
class _ActiveRun:
    session_id: str
    task: asyncio.Task[Any]
    finished: asyncio.Event


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        subagent_registry: BackgroundTaskRegistry | None = None,
        runtime_service: RuntimeService | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._subagent_registry = subagent_registry
        self._runtime = runtime_service
        self._runtime_bootstrapped = False
        self._runtime_bootstrap_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_runs: dict[str, _ActiveRun] = {}
        self._skill_loader = SkillLoader()
        self._rehydrate()

    # 首次异步操作前将文件 session 索引幂等导入 runtime
    async def _ensure_runtime_sessions(self) -> None:
        if self._runtime is None or self._runtime_bootstrapped:
            return
        async with self._runtime_bootstrap_lock:
            if self._runtime_bootstrapped:
                return
            await self._runtime.bootstrap_sessions(list(self._sessions.values()))
            self._runtime_bootstrapped = True

    # 从磁盘恢复会话索引；active 表示 daemon 在一次 run 中退出，恢复为 interrupted
    def _rehydrate(self) -> None:
        for session in self._store.list_sessions():
            if session.status == "active":
                self._store.recover_incomplete_tail(session.id)
                session.status = "interrupted"
                self._store.write_meta(session)
            self._sessions[session.id] = session
            self._locks[session.id] = asyncio.Lock()

    # 创建新 session 并写入 meta.json
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        await self._ensure_runtime_sessions()
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        if self._runtime is not None:
            await self._runtime.sync_session(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(
        self,
        sid: str,
        content: str,
        *,
        run_id: str | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
    ) -> str:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status in ("waiting_for_input", "interrupted"):
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            run_id = run_id or new_run_id()
            self._store.append_message(
                sid,
                "user",
                content,
                run_id=run_id,
                message_id=f"{run_id}:user",
            )
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40]

            session.run_ids.append(run_id)
            session.status = "active"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.start_turn(
                    session,
                    run_id,
                    content,
                    runtime_mode=runtime_mode,
                )

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = self._skill_loader.resolve(skill_name)
                if skill is not None:
                    goal = self._skill_loader.render_prompt(skill, arguments)
                    system_prompt_override = skill.system_prompt_template
                    tool_whitelist = skill.allowed_tools or None
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )

            runner = self._runner_factory()
            runner_task = asyncio.create_task(
                runner.run_and_capture(
                    goal,
                    run_id=run_id,
                    session=session,
                    store=self._store,
                    system_prompt_override=system_prompt_override,
                    tool_whitelist=tool_whitelist,
                    runtime_mode=runtime_mode,
                ),
                name=f"run:{run_id}",
            )
            active = _ActiveRun(
                session_id=sid,
                task=runner_task,
                finished=asyncio.Event(),
            )
            self._active_runs[run_id] = active
            try:
                outcome = await runner_task
            except asyncio.CancelledError:
                session.status = "interrupted"
                session.updated_at = _now()
                self._store.write_meta(session)
                if self._runtime is not None:
                    await self._runtime.finish_turn(
                        session,
                        run_id,
                        TurnStatus.INTERRUPTED,
                        reason="cancelled",
                    )
                await self._bus.publish(
                    SessionInterruptedEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        reason="cancelled",
                        ts=session.updated_at,
                    )
                )
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return run_id
            finally:
                self._active_runs.pop(run_id, None)
                active.finished.set()

            session.updated_at = _now()
            if (
                runtime_mode == RuntimeMode.PLAN
                and outcome.status == "success"
                and outcome.result.strip()
            ):
                await self._bus.publish(
                    PlanReadyEvent(
                        session_id=sid,
                        run_id=run_id,
                        request=content,
                        plan=outcome.result.strip(),
                        ts=session.updated_at,
                    )
                )
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            if self._runtime is not None:
                runtime_status = (
                    TurnStatus.COMPLETED
                    if outcome.status == "success"
                    else TurnStatus.FAILED
                )
                await self._runtime.finish_turn(
                    session,
                    run_id,
                    runtime_status,
                    reason=outcome.reason,
                )
            return run_id

    async def cancel_run(self, run_id: str) -> str:
        active = self._active_runs.get(run_id)
        if active is None or active.task.done():
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        if not active.task.cancel():
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        try:
            await active.task
        except asyncio.CancelledError:
            pass
        if self._subagent_registry is not None:
            await self._subagent_registry.cancel_descendants(run_id)
        await active.finished.wait()
        return active.session_id

    async def cancel_all(self) -> None:
        run_ids = list(self._active_runs)
        if not run_ids:
            return
        await asyncio.gather(
            *(self.cancel_run(run_id) for run_id in run_ids),
            return_exceptions=True,
        )

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from code_rook.core.bus.commands import SessionCompactResult
            from code_rook.core.compact.compactor import Compactor
            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid, store=self._store)
            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            await compactor.commit(
                result,
                run_id="manual",
                trigger="manual",
                publish=False,
            )
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.compacted_tokens),
                original_tokens=result.original_token_estimate,
                compacted_tokens=result.compacted_tokens,
                retained_tokens=result.retained_tokens,
                retained_messages=result.retained_messages,
                quality_score=result.quality.score,
                summary_path=result.summary_path,
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 返回已恢复的指定会话，供 Core 的会话级配置命令做存在性校验
    def get_session(self, sid: str) -> Session:
        return self._get_session(sid)

    # 返回最近更新的 session 元数据，供 CLI/TUI 选择历史会话
    async def list_sessions(
        self,
        *,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[Session]:
        await self._ensure_runtime_sessions()
        sessions = sorted(
            self._sessions.values(),
            key=lambda session: session.updated_at,
            reverse=True,
        )
        if not include_closed:
            sessions = [session for session in sessions if session.status != "closed"]
        return sessions[:limit]

    # 重新打开一个持久化 chat session，使后续消息沿用原 thread
    async def resume(self, sid: str) -> Session:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if session.mode != "chat":
            raise HandlerError(SESSION_NOT_RESUMABLE, "only chat sessions can be resumed")

        async with lock:
            session.status = "waiting_for_input"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=session.updated_at))
        return session

    async def rename(self, sid: str, title: str) -> Session:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        normalized = title.strip()
        if not normalized:
            raise HandlerError(INVALID_PARAMS, "session title must not be blank")
        async with lock:
            session.title = normalized
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(
                SessionRenamedEvent(
                    session_id=sid,
                    title=normalized,
                    ts=session.updated_at,
                )
            )
        return session

    async def fork(self, sid: str, title: str = "") -> Session:
        await self._ensure_runtime_sessions()
        source = self._get_session(sid)
        source_lock = self._locks[sid]
        if source_lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with source_lock:
            fork_id = f"sess-{uuid.uuid4().hex[:12]}"
            ts = _now()
            fork_title = title.strip() or f"{source.title or source.id} (fork)"
            forked = Session(
                id=fork_id,
                mode="chat",
                status="waiting_for_input",
                title=fork_title[:200],
                created_at=ts,
                updated_at=ts,
                run_ids=[],
                parent_session_id=source.id,
            )
            self._store.create_fork(source.id, forked)
            self._sessions[fork_id] = forked
            self._locks[fork_id] = asyncio.Lock()
            if self._runtime is not None:
                await self._runtime.sync_session(forked)
            await self._bus.publish(
                SessionCreatedEvent(session_id=fork_id, mode="chat", ts=ts)
            )
            await self._bus.publish(
                SessionForkedEvent(
                    session_id=fork_id,
                    source_session_id=source.id,
                    ts=ts,
                )
            )
        return forked

    async def export(
        self,
        sid: str,
        export_format: SessionExportFormat,
    ) -> tuple[str, str, str]:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            return export_session(
                session,
                self._store.read_messages(sid),
                self._store.read_notes(sid),
                export_format,
            )

    async def delete(self, sid: str) -> None:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            self._store.delete_session(sid)
        self._sessions.pop(sid, None)
        self._locks.pop(sid, None)
        if self._runtime is not None:
            await self._runtime.delete_session(sid)
        await self._bus.publish(SessionDeletedEvent(session_id=sid, ts=_now()))

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            try:
                session = self._store.read_meta(sid)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                raise HandlerError(SESSION_NOT_FOUND, "session not found") from None
            if session.status == "active":
                session.status = "interrupted"
                self._store.write_meta(session)
            self._sessions[sid] = session
            self._locks[sid] = asyncio.Lock()
        return session

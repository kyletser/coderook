from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from code_rook.core.authority import AuthoritySnapshot, detect_sandbox_capability
from code_rook.core.events.bus import EventBus
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    SessionFacadeRecord,
    ThreadRecord,
    ThreadStatus,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)
from code_rook.core.runtime.store import (
    RecordAlreadyExistsError,
    RecordNotFoundError,
    RuntimeStore,
)

if TYPE_CHECKING:
    from code_rook.core.session.model import Session

_SESSION_TO_THREAD_STATUS = {
    "active": ThreadStatus.RUNNING,
    "waiting_for_input": ThreadStatus.IDLE,
    "interrupted": ThreadStatus.INTERRUPTED,
    "closed": ThreadStatus.ARCHIVED,
}


# 将 session 时间文本解析为 datetime
def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class RuntimeService:
    # 初始化异步 runtime facade 并固定 workspace 投影
    def __init__(
        self,
        store: RuntimeStore,
        workspace: Path,
        *,
        bus: EventBus | None = None,
        boot_id: str | None = None,
        authority_provider: Callable[[str], AuthoritySnapshot] | None = None,
    ) -> None:
        self._store = store
        self._workspace = str(workspace.resolve())
        self._bus = bus
        self.boot_id = boot_id or f"boot-{uuid.uuid4().hex}"
        self._authority_provider = authority_provider
        self._default_authority = AuthoritySnapshot(
            sandbox=detect_sandbox_capability()
        )
        self._write_lock = asyncio.Lock()

    # 幂等导入历史 session 及其 run 索引
    async def bootstrap_sessions(self, sessions: list[Session]) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._bootstrap_sessions_sync, sessions)

    # 创建或同步单个 session 的 thread 投影
    async def sync_session(self, session: Session) -> ThreadRecord:
        async with self._write_lock:
            return await asyncio.to_thread(self._sync_session_sync, session)

    # 为用户消息创建 running turn、message item 和启动事件
    async def start_turn(
        self,
        session: Session,
        run_id: str,
        content: str,
    ) -> TurnRecord:
        authority = (
            self._authority_provider(session.id)
            if self._authority_provider is not None
            else self._default_authority
        )
        async with self._write_lock:
            turn, event = await asyncio.to_thread(
                self._start_turn_sync,
                session,
                run_id,
                content,
                authority,
            )
            await self._publish_runtime_event(event)
        return turn

    # 将 turn 原子转换到终态并同步 thread 投影
    async def finish_turn(
        self,
        session: Session,
        run_id: str,
        status: TurnStatus,
        *,
        reason: str | None = None,
    ) -> TurnRecord:
        async with self._write_lock:
            turn, event = await asyncio.to_thread(
                self._finish_turn_sync,
                session,
                run_id,
                status,
                reason,
            )
            await self._publish_runtime_event(event)
        return turn

    # 删除 session 对应的 runtime thread
    async def delete_session(self, session_id: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._store.delete_thread, session_id)

    # 异步查询 thread
    async def get_thread(self, thread_id: str) -> ThreadRecord:
        return await asyncio.to_thread(self._store.get_thread, thread_id)

    # 异步查询 turn
    async def get_turn(self, turn_id: str) -> TurnRecord:
        return await asyncio.to_thread(self._store.get_turn, turn_id)

    # 异步列出全部 thread
    async def list_threads(self) -> list[ThreadRecord]:
        return await asyncio.to_thread(self._store.list_threads)

    # 异步列出 thread 的全部 turn
    async def list_turns(self, thread_id: str) -> list[TurnRecord]:
        return await asyncio.to_thread(self._store.list_turns, thread_id)

    # 异步查询指定游标和高水位之间的 runtime 事件
    async def list_events(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        up_to_seq: int | None = None,
        limit: int = 1000,
    ) -> list[RuntimeEventRecord]:
        return await asyncio.to_thread(
            self._store.list_events,
            thread_id,
            after_seq=after_seq,
            up_to_seq=up_to_seq,
            limit=limit,
        )

    # 异步查询 thread 当前事件高水位
    async def latest_event_seq(self, thread_id: str) -> int:
        return await asyncio.to_thread(self._store.latest_event_seq, thread_id)

    # 恢复其他 daemon boot 遗留的活动 turn 并发布持久事件
    async def recover_stale_turns(self, ts: datetime) -> list[RuntimeEventRecord]:
        async with self._write_lock:
            events = await asyncio.to_thread(
                self._store.recover_stale_turns,
                self.boot_id,
                ts,
            )
            for event in events:
                await self._publish_runtime_event(event)
        return events

    # 异步列出 thread 的 item
    async def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        return await asyncio.to_thread(self._store.list_items, turn_id)

    # 同步批量导入历史 session
    def _bootstrap_sessions_sync(self, sessions: list[Session]) -> None:
        for session in sessions:
            self._sync_session_sync(session)
            for index, run_id in enumerate(session.run_ids):
                status = TurnStatus.COMPLETED
                if (
                    session.status == "interrupted"
                    and index == len(session.run_ids) - 1
                ):
                    status = TurnStatus.INTERRUPTED
                imported = TurnRecord(
                    id=run_id,
                    thread_id=session.id,
                    status=status,
                    created_at=_parse_time(session.created_at),
                    updated_at=_parse_time(session.updated_at),
                )
                try:
                    self._store.create_turn(imported)
                except RecordAlreadyExistsError:
                    continue

    # 同步创建或更新 session 的 thread 与兼容 facade
    def _sync_session_sync(self, session: Session) -> ThreadRecord:
        thread = ThreadRecord(
            id=session.id,
            title=session.title,
            workspace=self._workspace,
            status=_SESSION_TO_THREAD_STATUS[session.status],
            created_at=_parse_time(session.created_at),
            updated_at=_parse_time(session.updated_at),
        )
        self._store.upsert_thread(thread)
        self._store.upsert_session_facade(
            SessionFacadeRecord(
                thread_id=session.id,
                mode=session.mode,
                parent_thread_id=session.parent_session_id,
            )
        )
        return thread

    # 同步建立 turn 和首条用户消息记录
    def _start_turn_sync(
        self,
        session: Session,
        run_id: str,
        content: str,
        authority: AuthoritySnapshot,
    ) -> tuple[TurnRecord, RuntimeEventRecord]:
        self._sync_session_sync(session)
        now = _parse_time(session.updated_at)
        turn = TurnRecord(
            id=run_id,
            thread_id=session.id,
            status=TurnStatus.RUNNING,
            mode=authority.mode,
            authority_profile=authority.profile,
            workspace_trust=authority.workspace_trust,
            sandbox=authority.sandbox,
            allowed_actions=authority.allowed_actions,
            boot_id=self.boot_id,
            created_at=now,
            updated_at=now,
        )
        self._store.create_turn(turn)
        event = self._store.record_item_and_event(
            TurnItemRecord(
                id=f"{run_id}:user",
                turn_id=run_id,
                kind=TurnItemKind.MESSAGE,
                payload={"role": "user", "content": content},
                created_at=now,
            ),
            event_type="turn.started",
            event_payload={"status": TurnStatus.RUNNING.value},
            event_ts=now,
        )
        return turn, event

    # 同步完成 turn 并刷新所属 thread 状态
    def _finish_turn_sync(
        self,
        session: Session,
        run_id: str,
        status: TurnStatus,
        reason: str | None,
    ) -> tuple[TurnRecord, RuntimeEventRecord]:
        if status not in {
            TurnStatus.COMPLETED,
            TurnStatus.FAILED,
            TurnStatus.INTERRUPTED,
        }:
            raise ValueError(f"finish_turn requires terminal status, got {status.value}")
        now = _parse_time(session.updated_at)
        error: dict[str, JsonValue] | None = None
        if status != TurnStatus.COMPLETED:
            error = {"reason": reason or status.value}
        event = self._store.transition_turn_and_event(
            run_id,
            status=status,
            event_type=f"turn.{status.value}",
            event_payload={"status": status.value, "reason": reason},
            event_ts=now,
            error=error,
        )
        self._sync_session_sync(session)
        return self._store.get_turn(run_id), event

    # 将已持久化 runtime 事件投递到进程内 EventBus
    async def _publish_runtime_event(self, event: RuntimeEventRecord) -> None:
        if self._bus is None:
            return
        from code_rook.core.bus.events import RuntimeEventAppendedEvent

        await self._bus.publish(
            RuntimeEventAppendedEvent(
                thread_id=event.thread_id,
                turn_id=event.turn_id,
                seq=event.seq,
                event_type=event.type,
                payload=event.payload,
                ts=event.ts.isoformat(),
            )
        )

    # 查询 thread 是否已存在
    async def contains_thread(self, thread_id: str) -> bool:
        try:
            await self.get_thread(thread_id)
        except RecordNotFoundError:
            return False
        return True

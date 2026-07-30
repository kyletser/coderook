from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import JsonValue

from code_rook.core.runtime.models import (
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
    def __init__(self, store: RuntimeStore, workspace: Path) -> None:
        self._store = store
        self._workspace = str(workspace.resolve())
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
        async with self._write_lock:
            return await asyncio.to_thread(
                self._start_turn_sync,
                session,
                run_id,
                content,
            )

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
            return await asyncio.to_thread(
                self._finish_turn_sync,
                session,
                run_id,
                status,
                reason,
            )

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
    ) -> TurnRecord:
        self._sync_session_sync(session)
        now = _parse_time(session.updated_at)
        turn = TurnRecord(
            id=run_id,
            thread_id=session.id,
            status=TurnStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        self._store.create_turn(turn)
        self._store.record_item_and_event(
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
        return turn

    # 同步完成 turn 并刷新所属 thread 状态
    def _finish_turn_sync(
        self,
        session: Session,
        run_id: str,
        status: TurnStatus,
        reason: str | None,
    ) -> TurnRecord:
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
        self._store.transition_turn_and_event(
            run_id,
            status=status,
            event_type=f"turn.{status.value}",
            event_payload={"status": status.value, "reason": reason},
            event_ts=now,
            error=error,
        )
        self._sync_session_sync(session)
        return self._store.get_turn(run_id)

    # 查询 thread 是否已存在
    async def contains_thread(self, thread_id: str) -> bool:
        try:
            await self.get_thread(thread_id)
        except RecordNotFoundError:
            return False
        return True

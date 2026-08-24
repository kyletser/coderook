from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from code_rook.core.bus.events import (
    ContextCompactedEvent,
    LlmTokenEvent,
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    RunSteeredEvent,
    StepFinishedEvent,
    StepStartedEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)

if TYPE_CHECKING:
    from code_rook.core.audit import AuditHealth
    from code_rook.core.events.bus import EventBus
    from code_rook.core.session.store import SessionStore


class SessionLedgerBridge:
    # 初始化把运行事件投影进指定 Session 事实日志的有界桥接器
    def __init__(
        self,
        store: SessionStore,
        session_id: str,
        *,
        run_id: str = "",
        audit_health: AuditHealth | None = None,
        chunk_limit: int = 64 * 1024,
        flush_delay_s: float = 0.025,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id
        self._audit_health = audit_health
        self._chunk_limit = max(1024, chunk_limit)
        self._flush_delay_s = max(0.001, flush_delay_s)
        self._chunks: list[str] = []
        self._chunk_bytes = 0
        self._chunk_run_id = ""
        self._flush_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._bus: EventBus | None = None

    # 返回桥接器自身，供与 EventWriter 组合使用 async with
    async def __aenter__(self) -> SessionLedgerBridge:
        return self

    # 离开上下文时无条件刷新并撤销订阅
    async def __aexit__(self, *args: object) -> None:
        await self.close()

    # 将桥接器注册为关键订阅者，使非流式事实持久化失败时阻止继续广播
    def subscribe(self, bus: EventBus) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self.handle)
        self._bus = bus
        bus.subscribe(self.handle, first=True, critical=True)

    # 刷新剩余流片段并撤销总线订阅
    async def close(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        await self.flush()
        if self._bus is not None:
            self._bus.unsubscribe(self.handle)
            self._bus = None

    # 将事件转换为一个或多个事实事件，流式 token 先有界聚合
    async def handle(self, event: BaseModel) -> None:
        if self._run_id and not _belongs_to_run(event, self._run_id):
            return
        if isinstance(event, LlmTokenEvent):
            await self._buffer_chunk(event)
            return
        await self.flush()
        for event_type, turn_id, step_id, payload in _project_event(event):
            ledger_seq = await self._append(
                event_type,
                turn_id=turn_id,
                step_id=step_id,
                payload=payload,
            )
            if hasattr(event, "ledger_seq") and getattr(event, "ledger_seq") is None:
                setattr(event, "ledger_seq", ledger_seq)

    # 把 token 放入有界缓冲并按时间或字节阈值安排落盘
    async def _buffer_chunk(self, event: LlmTokenEvent) -> None:
        async with self._lock:
            self._chunks.append(event.token)
            self._chunk_bytes += len(event.token.encode("utf-8"))
            self._chunk_run_id = event.run_id
            if self._chunk_bytes >= self._chunk_limit:
                await self._flush_locked()
                return
            if self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._delayed_flush())

    # 等待很短的聚合窗口后提交当前流片段
    async def _delayed_flush(self) -> None:
        try:
            await asyncio.sleep(self._flush_delay_s)
            await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._audit_health is not None:
                await self._audit_health.degrade("session_ledger", exc)

    # 立即提交当前流片段，写入失败时保留批次供下一次重试
    async def flush(self) -> None:
        async with self._lock:
            await self._flush_locked()

    # 在已持有锁时把聚合 chunk 作为单条 llm.chunk 事件写入
    async def _flush_locked(self) -> None:
        if not self._chunks:
            return
        text = "".join(self._chunks)
        run_id = self._chunk_run_id
        try:
            self._store.append_session_event(
                self._session_id,
                event_type="llm.chunk",
                turn_id=run_id,
                payload={"text": text, "chunk_count": len(self._chunks)},
            )
        except Exception as exc:
            if self._audit_health is not None:
                await self._audit_health.degrade("session_ledger", exc)
            raise
        else:
            self._chunks.clear()
            self._chunk_bytes = 0
            self._chunk_run_id = ""

    # 耐久追加单个事实事件并在失败时切换审计降级状态
    async def _append(
        self,
        event_type: str,
        *,
        turn_id: str,
        step_id: str,
        payload: dict[str, Any],
    ) -> int:
        try:
            appended = self._store.append_session_event(
                self._session_id,
                event_type=event_type,
                turn_id=turn_id,
                step_id=step_id,
                payload=payload,
            )
        except Exception as exc:
            if self._audit_health is not None:
                await self._audit_health.degrade("session_ledger", exc)
            raise
        return appended.seq


# 把公开运行事件投影为稳定 Session 事实词汇
def _project_event(
    event: BaseModel,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    payload = event.model_dump(mode="json", exclude={"type", "ts"})
    run_id = str(payload.get("run_id", ""))
    raw_step = payload.get("step")
    step_id = f"{run_id}:{raw_step}" if run_id and isinstance(raw_step, int) else ""
    if isinstance(event, RunStartedEvent):
        return [("turn.started", event.run_id, "", payload)]
    if isinstance(event, RunFinishedEvent):
        return [
            ("run.outcome", event.run_id, "", payload),
            ("turn.finished", event.run_id, "", payload),
        ]
    if isinstance(event, StepStartedEvent):
        return [("step.started", event.run_id, step_id, payload)]
    if isinstance(event, StepFinishedEvent):
        return [("step.finished", event.run_id, step_id, payload)]
    if isinstance(event, ToolCallStartedEvent):
        return [("tool.call_started", event.run_id, step_id, payload)]
    if isinstance(event, ToolCallFinishedEvent):
        return [("tool.call_finished", event.run_id, step_id, payload)]
    if isinstance(event, ToolCallFailedEvent):
        return [("tool.call_failed", event.run_id, step_id, payload)]
    if isinstance(event, PermissionRequestedEvent):
        return [("permission.requested", event.run_id, step_id, payload)]
    if isinstance(event, (PermissionGrantedEvent, PermissionDeniedEvent)):
        return [("permission.resolved", event.run_id, step_id, payload)]
    if isinstance(event, RunSteeredEvent):
        return [("steer.admitted", event.run_id, step_id, payload)]
    if isinstance(event, ContextCompactedEvent):
        return [("context.compacted", event.run_id, step_id, payload)]
    if isinstance(event, SubagentStartedEvent):
        return [("worker.started", event.parent_run_id, "", payload)]
    if isinstance(event, SubagentFinishedEvent):
        return [("worker.finished", event.parent_run_id, "", payload)]
    return []


# 判断共享总线事件是否属于当前 run 或其直接子 Worker
def _belongs_to_run(event: BaseModel, run_id: str) -> bool:
    raw_run_id = getattr(event, "run_id", "")
    if raw_run_id == run_id:
        return True
    return getattr(event, "parent_run_id", "") == run_id

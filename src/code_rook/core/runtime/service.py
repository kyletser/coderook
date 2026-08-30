from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from pydantic import BaseModel, JsonValue, TypeAdapter

from code_rook.core.audit import AuditHealth
from code_rook.core.authority import (
    AuthoritySnapshot,
    RuntimeMode,
    detect_sandbox_capability,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.pricing import estimate_cost, resolve_pricing_quote
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.receipts.builder import build_turn_receipt
from code_rook.core.receipts.models import TurnReceipt
from code_rook.core.runtime.models import (
    QueuedMessageRecord,
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
    from code_rook.core.task.models import TaskTimelineEntry

_SESSION_TO_THREAD_STATUS = {
    "active": ThreadStatus.RUNNING,
    "waiting_for_input": ThreadStatus.IDLE,
    "interrupted": ThreadStatus.INTERRUPTED,
    "closed": ThreadStatus.ARCHIVED,
}
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_IGNORED_BUS_EVENTS = {"llm.token", "runtime.event_appended"}
logger = logging.getLogger(__name__)
_P = ParamSpec("_P")
_R = TypeVar("_R")


# 将 session 时间文本解析为 datetime
def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# 将任意 JSON 标量安全转换成非负计数，复杂值按零处理
def _json_count(value: JsonValue | None) -> int:
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


# 将 JSON 值收窄为可安全追加的列表
def _json_list(value: JsonValue | None) -> list[JsonValue]:
    return list(value) if isinstance(value, list) else []


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
        audit_health: AuditHealth | None = None,
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
        self._pending_writes: set[asyncio.Task[None]] = set()
        self._audit_health = audit_health

    # 在线程执行持久化写操作，并在任何存储异常后进入全局失败关闭状态
    async def _run_persistent_write(
        self,
        operation: Callable[_P, _R],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except Exception as exc:
            if self._audit_health is not None:
                await self._audit_health.degrade("runtime_projection", exc)
            raise

    # 幂等导入历史 session 及其 run 索引，可用 transcript 时间戳恢复真实 turn 时间
    async def bootstrap_sessions(
        self,
        sessions: list[Session],
        turn_times: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        async with self._write_lock:
            await self._run_persistent_write(
                self._bootstrap_sessions_sync, sessions, turn_times
            )

    # 创建或同步单个 session 的 thread 投影
    async def sync_session(self, session: Session) -> ThreadRecord:
        async with self._write_lock:
            return await self._run_persistent_write(self._sync_session_sync, session)

    # 为用户消息创建 running turn、message item 和启动事件
    async def start_turn(
        self,
        session: Session,
        run_id: str,
        content: str,
        *,
        runtime_mode: RuntimeMode | None = None,
        display_content: str | None = None,
        route: RouteReceipt | None = None,
    ) -> TurnRecord:
        authority = (
            self._authority_provider(session.id)
            if self._authority_provider is not None
            else self._default_authority
        )
        if runtime_mode is not None:
            authority = authority.model_copy(update={"mode": runtime_mode})
        async with self._write_lock:
            turn, event = await self._run_persistent_write(
                self._start_turn_sync,
                session,
                run_id,
                content,
                display_content,
                authority,
                route,
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
        result: str = "",
    ) -> TurnRecord:
        await self.drain_pending_writes()
        async with self._write_lock:
            if result.strip():
                message_event = await self._run_persistent_write(
                    self._append_assistant_message_sync,
                    run_id,
                    result,
                    _parse_time(session.updated_at),
                )
                await self._publish_runtime_event(message_event)
            turn, event = await self._run_persistent_write(
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
            await self._run_persistent_write(self._store.delete_thread, session_id)

    # 持久化跨前端共享的后续消息并广播队列变化
    async def enqueue_message(self, record: QueuedMessageRecord) -> None:
        async with self._write_lock:
            await self._run_persistent_write(self._store.enqueue_message, record)
            event = await self._run_persistent_write(
                self._store.append_event,
                thread_id=record.thread_id,
                turn_id=None,
                event_type="queue.message_added",
                payload={
                    "queue_id": record.id,
                    "content": record.display_content,
                    "mode": record.mode.value,
                    "attachment_count": len(record.attachments),
                    "status": record.status,
                },
                ts=record.created_at,
            )
            await self._publish_runtime_event(event)

    # 查询指定 thread 的持久消息队列
    async def list_queued_messages(self, thread_id: str) -> list[QueuedMessageRecord]:
        return await asyncio.to_thread(self._store.list_queued_messages, thread_id)

    # 原子领取队首消息并广播正在派发状态
    async def claim_next_queued_message(
        self,
        thread_id: str,
        ts: datetime,
    ) -> QueuedMessageRecord | None:
        async with self._write_lock:
            record = await self._run_persistent_write(
                self._store.claim_next_queued_message,
                thread_id,
                ts,
            )
            if record is None:
                return None
            event = await self._run_persistent_write(
                self._store.append_event,
                thread_id=thread_id,
                turn_id=None,
                event_type="queue.message_dispatching",
                payload={"queue_id": record.id, "status": "dispatching"},
                ts=ts,
            )
            await self._publish_runtime_event(event)
            return record

    # 将无法启动的排队消息保留为 blocked 并发布可恢复原因
    async def block_queued_message(
        self,
        record: QueuedMessageRecord,
        error: str,
        ts: datetime,
    ) -> None:
        async with self._write_lock:
            await self._run_persistent_write(
                self._store.block_queued_message,
                record.id,
                error,
                ts,
            )
            event = await self._run_persistent_write(
                self._store.append_event,
                thread_id=record.thread_id,
                turn_id=None,
                event_type="queue.message_blocked",
                payload={"queue_id": record.id, "status": "blocked", "error": error},
                ts=ts,
            )
            await self._publish_runtime_event(event)

    # 将用户确认重试的 blocked 消息恢复为 queued 并广播状态
    async def retry_queued_message(
        self,
        thread_id: str,
        message_id: str,
        ts: datetime,
    ) -> None:
        async with self._write_lock:
            await self._run_persistent_write(
                self._store.retry_queued_message,
                thread_id,
                message_id,
                ts,
            )
            event = await self._run_persistent_write(
                self._store.append_event,
                thread_id=thread_id,
                turn_id=None,
                event_type="queue.message_retried",
                payload={"queue_id": message_id, "status": "queued"},
                ts=ts,
            )
            await self._publish_runtime_event(event)

    # 移除完成或用户取消的排队消息并同步广播
    async def remove_queued_message(
        self,
        thread_id: str,
        message_id: str,
        *,
        reason: str,
        ts: datetime,
    ) -> None:
        async with self._write_lock:
            await self._run_persistent_write(
                self._store.delete_queued_message,
                thread_id,
                message_id,
            )
            event = await self._run_persistent_write(
                self._store.append_event,
                thread_id=thread_id,
                turn_id=None,
                event_type="queue.message_removed",
                payload={"queue_id": message_id, "reason": reason},
                ts=ts,
            )
            await self._publish_runtime_event(event)

    # daemon 启动时恢复崩溃窗口内尚未确认的队列领取记录
    async def recover_queued_messages(self, ts: datetime) -> int:
        async with self._write_lock:
            return await self._run_persistent_write(
                self._store.recover_queued_messages,
                ts,
            )

    # 异步查询 thread
    async def get_thread(self, thread_id: str) -> ThreadRecord:
        return await asyncio.to_thread(self._store.get_thread, thread_id)

    # 异步查询 turn
    async def get_turn(self, turn_id: str) -> TurnRecord:
        return await asyncio.to_thread(self._store.get_turn, turn_id)

    # 从 durable usage 投影读取已知估算成本，未知或未建 turn 时返回 None
    async def get_estimated_cost(self, turn_id: str) -> float | None:
        try:
            turn = await self.get_turn(turn_id)
        except RecordNotFoundError:
            return None
        value = turn.usage.get("estimated_cost_usd")
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
        return None

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

    # 构造 Task timeline 到当前 thread/turn 的持久 runtime event 投影
    def task_event_sink(
        self,
        thread_id: str,
        turn_id: str,
    ) -> Callable[[TaskTimelineEntry], None]:
        # 在 task 文件成功保存后把 SQLite 投影排入非阻塞后台写入
        def receive(entry: TaskTimelineEntry) -> None:
            # 在专用线程完成 SQLite 写入并按 seq 发布事件
            async def persist() -> None:
                async with self._write_lock:
                    event = await self._run_persistent_write(
                        self._store.append_event,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        event_type=entry.event,
                        payload={
                            "task_id": entry.task_id,
                            "timeline_seq": entry.seq,
                            "actor": entry.actor,
                            "details": entry.details,
                        },
                        ts=_parse_time(entry.at),
                    )
                    await self._publish_runtime_event(event)

            task = asyncio.create_task(
                persist(),
                name=f"runtime-task-event:{thread_id}:{turn_id}:{entry.seq}",
            )
            self._pending_writes.add(task)

        return receive

    # 等待所有已排队的 runtime SQLite 写入完成并传播存储错误
    async def drain_pending_writes(self) -> None:
        while self._pending_writes:
            pending = set(self._pending_writes)
            try:
                await asyncio.gather(*pending)
            finally:
                self._pending_writes.difference_update(pending)

    # 恢复其他 daemon boot 遗留的活动 turn 并发布持久事件
    async def recover_stale_turns(self, ts: datetime) -> list[RuntimeEventRecord]:
        async with self._write_lock:
            events = await self._run_persistent_write(
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

    # 异步列出单个 turn 的全部 durable events
    async def list_turn_events(self, turn_id: str) -> list[RuntimeEventRecord]:
        return await asyncio.to_thread(self._store.list_turn_events, turn_id)

    # 从持久化记录构建可在 daemon 重启后离线读取的 turn receipt
    async def get_receipt(self, turn_id: str) -> TurnReceipt:
        turn = await self.get_turn(turn_id)
        thread = await self.get_thread(turn.thread_id)
        items = await self.list_items(turn_id)
        events = await self.list_turn_events(turn_id)
        return build_turn_receipt(turn, items, events, workspace=thread.workspace)

    # 将运行中的领域事件投影到同一 durable runtime ledger
    async def record_bus_event(self, event: BaseModel) -> None:
        event_type = str(getattr(event, "type", ""))
        run_id = getattr(event, "run_id", None)
        if event_type in _IGNORED_BUS_EVENTS or not isinstance(run_id, str) or not run_id:
            return
        async with self._write_lock:
            try:
                persisted = await self._run_persistent_write(
                    self._record_bus_event_sync,
                    event,
                )
            except RecordNotFoundError:
                return
            except RecordAlreadyExistsError:
                logger.debug(
                    "runtime event already persisted type=%s run_id=%s",
                    event_type,
                    run_id,
                )
                return
        if persisted is not None:
            await self._publish_runtime_event(persisted)

    # 同步批量导入历史 session；优先采用 transcript 真实时间戳恢复 turn 时间
    def _bootstrap_sessions_sync(
        self,
        sessions: list[Session],
        turn_times: Mapping[str, tuple[str, str]] | None = None,
    ) -> None:
        times = turn_times or {}
        had_threads = bool(self._store.list_threads())
        imported_count = 0
        for session in sessions:
            self._sync_session_sync(session)
            for index, run_id in enumerate(session.run_ids):
                status = TurnStatus.COMPLETED
                if (
                    session.status == "interrupted"
                    and index == len(session.run_ids) - 1
                ):
                    status = TurnStatus.INTERRUPTED
                range_ts = times.get(run_id)
                created_at = (
                    _parse_time(range_ts[0])
                    if range_ts is not None
                    else _parse_time(session.created_at)
                )
                updated_at = (
                    _parse_time(range_ts[1])
                    if range_ts is not None
                    else _parse_time(session.updated_at)
                )
                imported = TurnRecord(
                    id=run_id,
                    thread_id=session.id,
                    status=status,
                    created_at=created_at,
                    updated_at=updated_at,
                )
                try:
                    self._store.create_turn(imported)
                    imported_count += 1
                except RecordAlreadyExistsError:
                    continue
        if not had_threads and imported_count:
            logger.warning(
                "runtime projection rebuilt from file ledger: imported %d turn(s) "
                "across %d session(s)",
                imported_count,
                len(sessions),
            )

    # 同步创建或更新 session 的 thread 与兼容 facade
    def _sync_session_sync(self, session: Session) -> ThreadRecord:
        status = _SESSION_TO_THREAD_STATUS[session.status]
        if session.status == "active" and not session.run_ids:
            status = ThreadStatus.IDLE
        thread = ThreadRecord(
            id=session.id,
            title=session.title,
            workspace=self._workspace,
            status=status,
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
        display_content: str | None,
        authority: AuthoritySnapshot,
        route: RouteReceipt | None,
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
            route=route,
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
                payload={"role": "user", "content": display_content or content},
                created_at=now,
            ),
            event_type="turn.started",
            event_payload={
                "status": TurnStatus.RUNNING.value,
                "route": route.model_dump(mode="json") if route is not None else None,
            },
            event_ts=now,
        )
        return turn, event

    # 同步追加最终 assistant 正文并生成消息事件
    def _append_assistant_message_sync(
        self,
        run_id: str,
        content: str,
        ts: datetime,
    ) -> RuntimeEventRecord:
        return self._store.record_item_and_event(
            TurnItemRecord(
                id=f"{run_id}:assistant:final",
                turn_id=run_id,
                kind=TurnItemKind.MESSAGE,
                payload={"role": "assistant", "content": content},
                created_at=ts,
            ),
            event_type="message.completed",
            event_payload={"role": "assistant"},
            event_ts=ts,
        )

    # 同步把单个 EventBus 事件映射为 event、item 或 usage 更新
    def _record_bus_event_sync(self, event: BaseModel) -> RuntimeEventRecord | None:
        raw = json.loads(event.model_dump_json())
        payload = _JSON_OBJECT.validate_python(
            {key: value for key, value in raw.items() if key not in {"type", "run_id", "ts"}}
        )
        event_type = str(raw["type"])
        source_run_id = str(raw["run_id"])
        run_id = (
            str(raw.get("parent_run_id") or source_run_id)
            if event_type.startswith("subagent.")
            else source_run_id
        )
        if event_type.startswith("subagent."):
            payload["worker_run_id"] = source_run_id
        ts = _parse_time(str(raw["ts"]))
        turn = self._store.get_turn(run_id)
        if event_type == "llm.usage":
            usage = dict(turn.usage)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                previous = usage.get(key, 0)
                current = payload.get(key, 0)
                previous_count = int(previous) if isinstance(previous, (int, float)) else 0
                current_count = int(current) if isinstance(current, (int, float)) else 0
                usage[key] = previous_count + current_count
            usage["context_pct"] = payload.get("context_pct", 0.0)
            model = str(payload.get("model") or (turn.route.model if turn.route else ""))
            models: list[JsonValue] = [
                item for item in _json_list(usage.get("models")) if isinstance(item, str)
            ]
            if model and model not in models:
                models.append(model)
            usage["models"] = models
            quote = resolve_pricing_quote(model)
            if quote is None or usage.get("cost_status") == "unknown":
                usage["cost_status"] = "unknown"
                usage["estimated_cost_usd"] = "unknown"
            else:
                previous_cost = usage.get("estimated_cost_usd", 0.0)
                base_cost = (
                    float(previous_cost)
                    if isinstance(previous_cost, (int, float))
                    else 0.0
                )
                usage["estimated_cost_usd"] = base_cost + estimate_cost(
                    quote.pricing,
                    input_tokens=_json_count(payload.get("input_tokens")),
                    output_tokens=_json_count(payload.get("output_tokens")),
                    cache_read_tokens=_json_count(
                        payload.get("cache_read_input_tokens")
                    ),
                    cache_write_tokens=_json_count(
                        payload.get("cache_creation_input_tokens")
                    ),
                )
                usage["cost_status"] = "estimated"
                pricing_evidence: list[JsonValue] = [
                    dict(item)
                    for item in _json_list(usage.get("pricing"))
                    if isinstance(item, dict)
                ]
                evidence: dict[str, JsonValue] = {
                    "model": model,
                    "source": quote.source,
                    "effective_date": quote.effective_date,
                }
                if evidence not in pricing_evidence:
                    pricing_evidence.append(evidence)
                usage["pricing"] = pricing_evidence
            return self._store.update_usage_and_event(
                run_id,
                usage=usage,
                event_type=event_type,
                event_payload=payload,
                event_ts=ts,
            )
        if event_type == "tool.call_started":
            item = TurnItemRecord(
                id=f"{run_id}:tool-call:{payload['tool_use_id']}",
                turn_id=run_id,
                kind=TurnItemKind.TOOL_CALL,
                tool_call_id=str(payload["tool_use_id"]),
                payload={
                    "tool_name": payload["tool_name"],
                    "params": payload["params"],
                    "operation_id": payload.get("operation_id", ""),
                    "presentation": payload.get("presentation") or {},
                },
                created_at=ts,
            )
            return self._store.record_item_and_event(
                item,
                event_type=event_type,
                event_payload=payload,
                event_ts=ts,
            )
        if event_type in {"tool.call_finished", "tool.call_failed"}:
            if event_type == "tool.call_failed" and payload.get("terminal") is False:
                return self._store.append_event(
                    thread_id=turn.thread_id,
                    turn_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    ts=ts,
                )
            item = TurnItemRecord(
                id=f"{run_id}:tool-result:{payload['tool_use_id']}",
                turn_id=run_id,
                kind=TurnItemKind.TOOL_RESULT,
                tool_call_id=str(payload["tool_use_id"]),
                payload=payload,
                created_at=ts,
            )
            return self._store.record_item_and_event(
                item,
                event_type=event_type,
                event_payload=payload,
                event_ts=ts,
            )
        return self._store.append_event(
            thread_id=turn.thread_id,
            turn_id=run_id,
            event_type=event_type,
            payload=payload,
            ts=ts,
        )

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

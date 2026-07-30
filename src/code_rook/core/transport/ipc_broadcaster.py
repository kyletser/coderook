from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from code_rook.core.bus.envelope import EventPushEnvelope
from code_rook.core.bus.events import RuntimeEventAppendedEvent
from code_rook.core.runtime.models import RuntimeEventRecord
from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.writer import TraceWriter

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _Subscription:
    sub_id: str
    writer: asyncio.StreamWriter
    topics: list[str]
    scope: str
    replaying_runtime: bool = False
    pending_runtime: list[dict[str, Any]] = field(default_factory=list)


class IpcEventBroadcaster:
    def __init__(self, trace: TraceWriter | None = None) -> None:
        self._subscriptions: list[_Subscription] = []
        self._trace = trace

    # 注册一个客户端订阅，返回 subscription_id
    def subscribe(
        self,
        writer: asyncio.StreamWriter,
        topics: list[str],
        scope: str = "global",
        *,
        replaying_runtime: bool = False,
    ) -> str:
        sub_id = f"sub-{uuid.uuid4().hex[:8]}"
        sub = _Subscription(
            sub_id=sub_id,
            writer=writer,
            topics=topics,
            scope=scope,
            replaying_runtime=replaying_runtime,
        )
        self._subscriptions.append(sub)
        return sub_id

    # 移除指定 writer 的所有订阅
    def unsubscribe(self, writer: asyncio.StreamWriter) -> None:
        self._subscriptions = [s for s in self._subscriptions if s.writer is not writer]

    # 将事件推送到所有匹配的订阅客户端，写入失败时延迟清理死连接
    async def handle(self, event: BaseModel) -> None:
        event_dict = event.model_dump()
        event_type: str = event_dict.get("type", "")
        run_id: str | None = event_dict.get("run_id")
        thread_id: str | None = event_dict.get("thread_id")
        topic_type = str(event_dict.get("event_type", event_type))

        dead: list[asyncio.StreamWriter] = []

        for sub in list(self._subscriptions):
            if not self._matches_topic(topic_type, sub.topics):
                continue
            if not self._matches_scope(run_id, thread_id, sub.scope):
                continue
            if sub.replaying_runtime and event_type == "runtime.event":
                sub.pending_runtime.append(event_dict)
                continue
            if not await self._send_event(sub, event_dict):
                dead.append(sub.writer)

        for writer in dead:
            self.unsubscribe(writer)

    # 向回放态订阅发送一批已持久化事件并返回实际发送数
    async def replay_runtime_batch(
        self,
        sub_id: str,
        events: list[RuntimeEventRecord],
    ) -> int:
        sub = self._find_subscription(sub_id)
        if sub is None:
            return 0
        count = 0
        for event in events:
            event_dict = self._runtime_event_dict(event)
            if not self._matches_topic(event.type, sub.topics):
                continue
            if not await self._send_event(sub, event_dict):
                self.unsubscribe(sub.writer)
                break
            count += 1
        return count

    # 刷新回放期间积压的实时事件并原子切换到直推模式
    async def finish_runtime_replay(
        self,
        sub_id: str,
        last_seq: int,
    ) -> tuple[int, int]:
        sub = self._find_subscription(sub_id)
        if sub is None:
            return 0, last_seq
        count = 0
        while True:
            pending = sub.pending_runtime
            sub.pending_runtime = []
            for event_dict in sorted(pending, key=lambda item: int(item["seq"])):
                seq = int(event_dict["seq"])
                if seq <= last_seq:
                    continue
                if not await self._send_event(sub, event_dict):
                    self.unsubscribe(sub.writer)
                    return count, last_seq
                last_seq = seq
                count += 1
            if not sub.pending_runtime:
                sub.replaying_runtime = False
                return count, last_seq

    # 按订阅标识查找当前活动订阅
    def _find_subscription(self, sub_id: str) -> _Subscription | None:
        return next(
            (subscription for subscription in self._subscriptions if subscription.sub_id == sub_id),
            None,
        )

    # 将持久化 runtime 记录转换为统一 IPC 事件字典
    @staticmethod
    def _runtime_event_dict(event: RuntimeEventRecord) -> dict[str, Any]:
        return RuntimeEventAppendedEvent(
            thread_id=event.thread_id,
            turn_id=event.turn_id,
            seq=event.seq,
            event_type=event.type,
            payload=event.payload,
            ts=event.ts.isoformat(),
        ).model_dump()

    # 向单个订阅写入事件 envelope，连接失效时返回 false
    async def _send_event(
        self,
        sub: _Subscription,
        event_dict: dict[str, Any],
    ) -> bool:
        event_type = str(event_dict.get("type", ""))
        run_id = event_dict.get("run_id")
        thread_id = event_dict.get("thread_id")
        try:
            envelope = EventPushEnvelope(event=event_dict)
            sub.writer.write(envelope.model_dump_json().encode() + b"\n")
            await sub.writer.drain()
            if self._trace is not None:
                client_id = str(sub.writer.get_extra_info("peername", "<unknown>"))
                self._trace.emit(
                    TraceRecord(
                        ts=_now(),
                        direction="CORE→CLIENT",
                        layer="ipc",
                        kind="push",
                        run_id=run_id,
                        client_id=client_id,
                        data={
                            "sub_id": sub.sub_id,
                            "event_type": event_type,
                            "thread_id": thread_id,
                        },
                    )
                )
            return True
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("dead connection for sub %s, scheduling cleanup", sub.sub_id)
            return False

    # 检查事件类型是否匹配订阅的 topic 列表（支持 fnmatch glob 模式）
    @staticmethod
    def _matches_topic(event_type: str, topics: list[str]) -> bool:
        return any(fnmatch.fnmatch(event_type, pattern) for pattern in topics)

    # 检查事件标识是否匹配 global、run 或 thread 订阅范围
    @staticmethod
    def _matches_scope(
        run_id: str | None,
        thread_id: str | None,
        scope: str,
    ) -> bool:
        if scope == "global":
            return True
        if scope.startswith("run:"):
            return run_id == scope[4:]
        if scope.startswith("thread:"):
            return thread_id == scope[7:]
        return False

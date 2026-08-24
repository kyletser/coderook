from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

from code_rook.core.bus.events import (
    RunStartedEvent,
    RuntimeEventAppendedEvent,
    StepStartedEvent,
)
from code_rook.core.runtime.models import RuntimeEventRecord
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster


def _make_writer(*, drain_raises: Exception | None = None) -> asyncio.StreamWriter:
    writer = MagicMock(spec=asyncio.StreamWriter)
    if drain_raises is not None:
        writer.drain = AsyncMock(side_effect=drain_raises)
    else:
        writer.drain = AsyncMock()
    return cast(asyncio.StreamWriter, writer)


def _run_started(run_id: str = "r1") -> RunStartedEvent:
    return RunStartedEvent(run_id=run_id, goal="test", ts="2026-01-01T00:00:00Z")


# 功能：验证 subscribe 后 handle 将匹配 topic 的事件写入 writer，且内容是合法的 EventPushEnvelope
# 设计：用 MagicMock writer 捕获写入的字节，反序列化后断言 kind 和 event.type，排除对网络层的依赖
async def test_subscriber_receives_matching_event() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])

    await broadcaster.handle(_run_started())

    writer.write.assert_called_once()  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["kind"] == "event"
    assert data["event"]["type"] == "run.started"


# 功能：验证无订阅时 handle 不向任何 writer 写入数据
# 设计：创建 broadcaster 但不 subscribe，调用 handle 后断言 write 从未被调用，验证空 fan-out 的边界情况
async def test_no_subscription_no_write() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()

    await broadcaster.handle(_run_started())

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证 topic glob "step.*" 匹配 step.started 但不匹配 run.started
# 设计：向同一 broadcaster 发布两种事件，断言 write 只被调用一次，验证 fnmatch 语义的 glob 边界行为
async def test_topic_glob_matches_step_not_run() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["step.*"])

    step_event = StepStartedEvent(run_id="r1", step=1, ts="2026-01-01T00:00:00Z")
    run_event = _run_started()

    await broadcaster.handle(step_event)
    await broadcaster.handle(run_event)

    assert writer.write.call_count == 1  # type: ignore[attr-defined]
    data = json.loads(writer.write.call_args[0][0].rstrip(b"\n"))  # type: ignore[attr-defined]
    assert data["event"]["type"] == "step.started"


# 功能：验证 scope="global" 的订阅能收到任意 run_id 的事件
# 设计：发布两个不同 run_id 的事件，断言两次都写入，确认 global scope 不过滤 run_id 字段
async def test_scope_global_receives_all_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="global")

    await broadcaster.handle(_run_started("r1"))
    await broadcaster.handle(_run_started("r2"))

    assert writer.write.call_count == 2  # type: ignore[attr-defined]


# 功能：验证 scope="run:<id>" 只接收匹配 run_id 的事件，过滤其他 run_id
# 设计：订阅 scope="run:abc"，发布 run_id="abc" 和 run_id="xyz"，断言只写入一次，验证 run-specific scope 的过滤语义
async def test_scope_run_specific_filters_other_run_ids() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"], scope="run:abc")

    await broadcaster.handle(_run_started("abc"))
    await broadcaster.handle(_run_started("xyz"))

    assert writer.write.call_count == 1  # type: ignore[attr-defined]


# 功能：验证 unsubscribe 后 handle 不再向该 writer 发送事件
# 设计：先 subscribe 再 unsubscribe，再调用 handle，断言 write 从未被调用，验证订阅生命周期的正确性
async def test_unsubscribe_stops_delivery() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, topics=["run.*"])
    broadcaster.unsubscribe(writer)

    await broadcaster.handle(_run_started())

    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证按 subscription_id 取消时只能移除当前 writer 自己的订阅
# 设计：两个 writer 共享多个同 topic 订阅，先用外部 writer 越权取消再由 owner 取消，按最终写入次数证明其他订阅未受影响
async def test_unsubscribe_by_id_enforces_writer_ownership() -> None:
    broadcaster = IpcEventBroadcaster()
    owner = _make_writer()
    other = _make_writer()
    removed_id = broadcaster.subscribe(owner, topics=["run.*"])
    broadcaster.subscribe(owner, topics=["run.*"])
    broadcaster.subscribe(other, topics=["run.*"])

    assert broadcaster.unsubscribe(other, removed_id) is False
    assert broadcaster.unsubscribe(owner, removed_id) is True
    assert broadcaster.unsubscribe(owner, removed_id) is False

    await broadcaster.handle(_run_started())

    assert owner.write.call_count == 1  # type: ignore[attr-defined]
    assert other.write.call_count == 1  # type: ignore[attr-defined]


# 功能：验证写入失败（ConnectionResetError）后订阅自动移除，下次 handle 不再尝试写入
# 设计：drain() 抛出 ConnectionResetError 触发死连接清理；断言第二次 handle 时 write 未被调用；
#       第一次 write 在 drain 前已执行，call_count==1 是预期行为而非被测点
async def test_dead_connection_removed_after_failure() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer(drain_raises=ConnectionResetError())
    broadcaster.subscribe(writer, topics=["run.*"])

    event = _run_started()
    await broadcaster.handle(event)  # drain fails → subscription removed

    assert writer.write.call_count == 1  # type: ignore[attr-defined]

    writer.write.reset_mock()  # type: ignore[attr-defined]
    await broadcaster.handle(event)  # no subscribers remain
    writer.write.assert_not_called()  # type: ignore[attr-defined]


# 功能：验证 thread scope 按内部 runtime 事件类型和 thread_id 精确过滤
# 设计：发布同类型不同 thread 的包装事件，断言仅目标 thread 写入且 topic 不依赖外层 runtime.event
async def test_runtime_event_matches_inner_topic_and_thread_scope() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(
        writer,
        topics=["turn.*"],
        scope="thread:thread-1",
    )

    await broadcaster.handle(
        RuntimeEventAppendedEvent(
            thread_id="thread-1",
            turn_id="turn-1",
            seq=1,
            event_type="turn.started",
            payload={},
            ts="2026-01-01T00:00:00Z",
        )
    )
    await broadcaster.handle(
        RuntimeEventAppendedEvent(
            thread_id="thread-2",
            turn_id="turn-2",
            seq=1,
            event_type="turn.started",
            payload={},
            ts="2026-01-01T00:00:00Z",
        )
    )

    assert writer.write.call_count == 1  # type: ignore[attr-defined]


# 功能：global 订阅不再收到带 thread 归属的事件，避免多客户端 thread 事件互见（§20 #18）
# 设计：global 订阅 thread 事件断言零写入，而同一 thread 事件被 thread: 作用域订阅接收，证明隔离有效
async def test_scope_global_excludes_thread_events() -> None:
    broadcaster = IpcEventBroadcaster()
    global_writer = _make_writer()
    thread_writer = _make_writer()
    broadcaster.subscribe(global_writer, topics=["**"], scope="global")
    broadcaster.subscribe(thread_writer, topics=["**"], scope="thread:thread-1")

    await broadcaster.handle(
        RuntimeEventAppendedEvent(
            thread_id="thread-1",
            turn_id="turn-1",
            seq=1,
            event_type="turn.started",
            payload={},
            ts="2026-01-01T00:00:00Z",
        )
    )

    global_writer.write.assert_not_called()  # type: ignore[attr-defined]
    thread_writer.write.assert_called_once()  # type: ignore[attr-defined]


# 功能：验证 runtime 回放期间实时事件被缓冲，并在历史高水位后按 seq 去重衔接
# 设计：先缓冲重复 seq=1 和新 seq=2，再回放 seq=1，完成阶段应只追加 seq=2
async def test_runtime_replay_buffers_and_deduplicates_live_events() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    sub_id = broadcaster.subscribe(
        writer,
        topics=["turn.*"],
        scope="thread:thread-1",
        replaying_runtime=True,
    )
    for seq, event_type in [(1, "turn.started"), (2, "turn.completed")]:
        await broadcaster.handle(
            RuntimeEventAppendedEvent(
                thread_id="thread-1",
                turn_id="turn-1",
                seq=seq,
                event_type=event_type,
                payload={},
                ts="2026-01-01T00:00:00Z",
            )
        )
    historical = RuntimeEventRecord(
        thread_id="thread-1",
        turn_id="turn-1",
        seq=1,
        type="turn.started",
        payload={},
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )

    replayed = await broadcaster.replay_runtime_batch(sub_id, [historical])
    pending, last_seq = await broadcaster.finish_runtime_replay(sub_id, 1)

    pushed = [
        json.loads(call.args[0].rstrip(b"\n"))["event"]["seq"]
        for call in writer.write.call_args_list  # type: ignore[attr-defined]
    ]
    assert replayed == 1
    assert pending == 1
    assert last_seq == 2
    assert pushed == [1, 2]


# 功能：验证 runtime 回放发送失败只撤销失败订阅，不清空同 writer 的全局订阅
# 设计：同一 writer 同时持有 global 与 replay 订阅，让 replay drain 失败后恢复 writer，再发布 global 事件确认仍可接收
async def test_runtime_replay_failure_preserves_other_writer_subscriptions() -> None:
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer(drain_raises=ConnectionResetError())
    broadcaster.subscribe(writer, topics=["run.*"], scope="global")
    replay_id = broadcaster.subscribe(
        writer,
        topics=["turn.*"],
        scope="thread:thread-1",
        replaying_runtime=True,
    )
    historical = RuntimeEventRecord(
        thread_id="thread-1",
        turn_id="turn-1",
        seq=1,
        type="turn.started",
        payload={},
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )

    replayed = await broadcaster.replay_runtime_batch(replay_id, [historical])
    writer.write.reset_mock()  # type: ignore[attr-defined]
    writer.drain = AsyncMock()
    await broadcaster.handle(_run_started())

    assert replayed == 0
    writer.write.assert_called_once()  # type: ignore[attr-defined]

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.app import CoreApp
from code_rook.core.bus.commands import EventSubscribeCommand
from code_rook.core.bus.events import RunStartedEvent
from code_rook.core.runtime.models import RuntimeEventRecord
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster


# 构造可记录事件写入且 drain 成功的 StreamWriter 替身
def _make_writer() -> asyncio.StreamWriter:
    writer = MagicMock(spec=asyncio.StreamWriter)
    writer.drain = AsyncMock()
    return cast(asyncio.StreamWriter, writer)


# 功能：验证 event.unsubscribe handler 只能删除当前连接 writer 自己的指定订阅
# 设计：先从另一 writer 请求删除 owner 的标识，再由 owner 删除并发布事件，以 removed 结果和写入次数共同证明归属边界
async def test_event_unsubscribe_handler_enforces_current_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = CoreApp()
    broadcaster = IpcEventBroadcaster()
    owner = _make_writer()
    other = _make_writer()
    owner_id = broadcaster.subscribe(owner, ["run.*"])
    broadcaster.subscribe(other, ["run.*"])
    app._broadcaster = broadcaster  # type: ignore[attr-defined]

    monkeypatch.setattr("code_rook.core.app.get_connection_writer", lambda: other)
    foreign = await app._unsubscribe_handler(  # type: ignore[attr-defined]
        {"subscription_id": owner_id}
    )
    monkeypatch.setattr("code_rook.core.app.get_connection_writer", lambda: owner)
    owned = await app._unsubscribe_handler(  # type: ignore[attr-defined]
        {"subscription_id": owner_id}
    )
    await broadcaster.handle(
        RunStartedEvent(run_id="run-1", goal="test", ts="2026-08-24T00:00:00Z")
    )

    assert foreign.removed is False
    assert owned.removed is True
    owner.write.assert_not_called()  # type: ignore[attr-defined]
    other.write.assert_called_once()  # type: ignore[attr-defined]


# 功能：验证 runtime thread 订阅初始化失败时只回滚新订阅并保留同 writer 的全局订阅
# 设计：让 runtime 高水位查询确定性抛错，随后向同一 broadcaster 发布 global 事件，证明异常清理没有清空 writer 全部订阅
async def test_runtime_subscribe_failure_preserves_existing_writer_subscription() -> None:
    class _FailingRuntime:
        # 在新 thread 订阅完成注册后模拟 runtime 投影查询失败
        async def latest_event_seq(self, _thread_id: str) -> int:
            raise RuntimeError("runtime unavailable")

    app = CoreApp()
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    broadcaster.subscribe(writer, ["run.*"], scope="global")
    app._broadcaster = broadcaster  # type: ignore[attr-defined]
    app._runtime = _FailingRuntime()  # type: ignore[assignment]
    command = EventSubscribeCommand(
        topics=["turn.*"],
        thread_id="thread-1",
        after_seq=0,
    )

    with pytest.raises(RuntimeError, match="runtime unavailable"):
        await app._subscribe_runtime_events(command, writer)  # type: ignore[attr-defined]
    await broadcaster.handle(
        RunStartedEvent(run_id="run-1", goal="test", ts="2026-08-24T00:00:00Z")
    )

    writer.write.assert_called_once()  # type: ignore[attr-defined]


# 功能：验证 runtime 回放写入断线后不会返回可被客户端误认作已确认的高水位
# 设计：让首条历史事件 drain 失败并保留同 writer 的 global 订阅，断言 subscribe 抛错且恢复 writer 后 global 通道仍可发送
async def test_runtime_replay_delivery_failure_rejects_high_water_ack() -> None:
    event = RuntimeEventRecord(
        thread_id="thread-1",
        turn_id="turn-1",
        seq=1,
        type="turn.started",
        payload={},
        ts=datetime(2026, 8, 24, tzinfo=UTC),
    )

    class _Runtime:
        # 返回一条确定的历史事件以触发 replay 写入路径
        async def latest_event_seq(self, _thread_id: str) -> int:
            return 1

        # 返回高水位范围内的唯一历史事件
        async def list_events(self, *args: object, **kwargs: object) -> list[RuntimeEventRecord]:
            return [event]

    app = CoreApp()
    broadcaster = IpcEventBroadcaster()
    writer = _make_writer()
    writer.drain = AsyncMock(side_effect=ConnectionResetError())
    broadcaster.subscribe(writer, ["run.*"], scope="global")
    app._broadcaster = broadcaster  # type: ignore[attr-defined]
    app._runtime = _Runtime()  # type: ignore[assignment]
    command = EventSubscribeCommand(
        topics=["turn.*"],
        thread_id="thread-1",
        after_seq=0,
    )

    with pytest.raises(ConnectionError, match="replay delivery failed"):
        await app._subscribe_runtime_events(command, writer)  # type: ignore[attr-defined]

    writer.write.reset_mock()  # type: ignore[attr-defined]
    writer.drain = AsyncMock()
    await broadcaster.handle(
        RunStartedEvent(run_id="run-1", goal="test", ts="2026-08-24T00:00:00Z")
    )

    writer.write.assert_called_once()  # type: ignore[attr-defined]

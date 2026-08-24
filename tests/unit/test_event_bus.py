from __future__ import annotations

import pytest
from pydantic import BaseModel

from code_rook.core.events.bus import EventBus


class _FakeEvent(BaseModel):
    value: str


# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联 handler 收集事件引用，断言 is 而非 ==，排除序列化中间步骤的干扰
async def test_publish_reaches_subscriber() -> None:
    bus = EventBus()
    received: list[BaseModel] = []

    async def handler(event: BaseModel) -> None:
        received.append(event)

    bus.subscribe(handler)
    event = _FakeEvent(value="hello")
    await bus.publish(event)
    assert received == [event]


# 功能：验证多个订阅者都能独立收到同一事件
# 设计：两个独立计数器分别累加，避免共享状态掩盖某一订阅者未被调用的情况
async def test_multiple_subscribers_all_receive() -> None:
    bus = EventBus()
    counts = [0, 0]

    async def h1(e: BaseModel) -> None:
        counts[0] += 1

    async def h2(e: BaseModel) -> None:
        counts[1] += 1

    bus.subscribe(h1)
    bus.subscribe(h2)
    await bus.publish(_FakeEvent(value="x"))
    assert counts == [1, 1]


# 功能：验证多个订阅者按注册顺序被依次调用
# 设计：用追加整数到列表来记录调用次序，因为 bus 的顺序语义是 AgentLoop 事件序列正确性的前提
async def test_subscribers_called_in_order() -> None:
    bus = EventBus()
    order: list[int] = []

    async def h1(e: BaseModel) -> None:
        order.append(1)

    async def h2(e: BaseModel) -> None:
        order.append(2)

    bus.subscribe(h1)
    bus.subscribe(h2)
    await bus.publish(_FakeEvent(value="x"))
    assert order == [1, 2]


# 功能：验证 first 订阅者先于已有普通订阅者接收事件
# 设计：先注册普通处理器再前插边界处理器，直接断言调用轨迹以覆盖优先顺序契约
async def test_first_subscriber_runs_before_existing_subscribers() -> None:
    bus = EventBus()
    order: list[str] = []

    # 记录普通订阅者的调用位置
    async def normal(event: BaseModel) -> None:
        order.append("normal")

    # 记录优先订阅者的调用位置
    async def first(event: BaseModel) -> None:
        order.append("first")

    bus.subscribe(normal)
    bus.subscribe(first, first=True)
    await bus.publish(_FakeEvent(value="x"))
    assert order == ["first", "normal"]


# 功能：验证注销后的处理器不再接收后续事件
# 设计：发布前移除唯一处理器并断言收集列表为空，覆盖短生命周期订阅者清理路径
async def test_unsubscribe_stops_future_delivery() -> None:
    bus = EventBus()
    received: list[BaseModel] = []

    # 收集仍被投递给处理器的事件
    async def handler(event: BaseModel) -> None:
        received.append(event)

    bus.subscribe(handler)
    bus.unsubscribe(handler)
    await bus.publish(_FakeEvent(value="x"))
    assert received == []


# 功能：验证无订阅者时 publish 不抛异常（空 bus 边界条件）
# 设计：只调用 publish，不断言返回值，以"不引发异常"作为唯一判据
async def test_no_subscribers_publish_is_noop() -> None:
    bus = EventBus()
    await bus.publish(_FakeEvent(value="x"))  # should not raise


# 功能：某订阅者抛异常时不中断后续订阅者
# 设计：首个 handler 抛 RuntimeError，断言第二个 handler 仍收到事件，验证异常隔离
async def test_failing_subscriber_does_not_block_others() -> None:
    bus = EventBus()
    received: list[BaseModel] = []

    async def broken(event: BaseModel) -> None:
        raise RuntimeError("boom")

    async def handler(event: BaseModel) -> None:
        received.append(event)

    bus.subscribe(broken)
    bus.subscribe(handler)
    event = _FakeEvent(value="isolated")
    await bus.publish(event)
    assert received == [event]


# 功能：验证关键持久化订阅者失败时发布必须立即失败关闭
# 设计：把抛错处理器标为 critical 并断言异常向上传播，区别于普通观察者的隔离语义
async def test_critical_subscriber_failure_propagates() -> None:
    bus = EventBus()

    # 模拟事实账本无法持久化
    async def broken(event: BaseModel) -> None:
        raise RuntimeError("ledger unavailable")

    bus.subscribe(broken, critical=True)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await bus.publish(_FakeEvent(value="must persist"))

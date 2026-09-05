from __future__ import annotations

import asyncio

from pydantic import BaseModel

from code_rook.core.bus.events import LlmTokenEvent
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.turn import StreamIdleTimeoutError, StreamWatchdog, WatchdogLimits


# 创建最小运行上下文以隔离 watchdog 行为
def _context() -> ExecutionContext:
    return ExecutionContext(run_id="watchdog-run", goal="test", max_steps=3)


class _NeverProvider:
    # 永久等待以模拟没有 token 也不结束的断流 Provider
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _ActiveForeverProvider:
    # 持续发布 token 但永不结束，以区分 wall timeout 与 idle timeout
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        bus = kwargs["bus"]
        run_id = str(kwargs["run_id"])
        assert isinstance(bus, EventBus)
        while True:
            await bus.publish(LlmTokenEvent(run_id=run_id, token="x", ts="2026-08-03T00:00:00Z"))
            await asyncio.sleep(0.002)


class _LargeResponseProvider:
    # 返回超过 watchdog 最大字节数的非流式完整响应
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        return LlmResponse(stop_reason="end_turn", text="x" * 10_000)


class _SequenceProvider:
    # 初始化可混合异常与响应的确定性 Provider 序列
    def __init__(self, values: list[LlmResponse | Exception]) -> None:
        self._values = iter(values)
        self.calls = 0

    # 依次抛出 transient 或返回空/非空响应
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        self.calls += 1
        value = next(self._values)
        if isinstance(value, Exception):
            raise value
        return value


# 功能：验证永不结束且无流活动的 Provider 在 idle 边界内进入明确失败状态
# 设计：使用 10ms idle 与 100ms wall，断言先命中 idle 原因且 Provider 任务被取消
async def test_never_ending_stream_fails_with_idle_timeout() -> None:
    loop = AgentLoop(
        _NeverProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        watchdog=StreamWatchdog(
            WatchdogLimits(
                idle_timeout_s=0.01,
                wall_timeout_s=0.1,
                max_response_bytes=1024,
            )
        ),
    )
    context = _context()

    await loop.run(context)

    assert context.status == "failed"
    assert context.reason == "stream_idle_timeout"


# 功能：验证未公开的 reasoning/tool SSE 增量也能刷新 watchdog 活动时间
# 设计：调用方只上报内部流字节而不发布 token，跨越原空闲阈值后正常返回以复现慢思考模型
async def test_private_stream_activity_prevents_false_idle_timeout() -> None:
    async def active_call(bus: EventBus) -> LlmResponse:
        for _ in range(3):
            await asyncio.sleep(0.015)
            bus.mark_stream_activity(1)
        return LlmResponse(stop_reason="end_turn", text="done")

    watchdog = StreamWatchdog(
        WatchdogLimits(
            idle_timeout_s=0.02,
            wall_timeout_s=0.2,
        )
    )

    response = await watchdog.run(active_call, EventBus())

    assert response.text == "done"


# 功能：验证调用方取消 watchdog 时内部活动等待任务也会同步回收
# 设计：让 Provider 永久等待后取消外层任务，再按显式任务名检查事件循环中没有残留 waiter
async def test_watchdog_cancellation_reaps_activity_waiter() -> None:
    async def never_finishes(_bus: EventBus) -> LlmResponse:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    watchdog = StreamWatchdog(
        WatchdogLimits(idle_timeout_s=10.0, wall_timeout_s=20.0)
    )
    call = asyncio.create_task(watchdog.run(never_finishes, EventBus()))
    await asyncio.sleep(0)
    call.cancel()

    try:
        await call
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name() == "stream-watchdog-activity"
        and not task.done()
    ]


# 功能：验证持续有 token 的无限流不会逃过 wall timeout
# 设计：让 idle 大于 token 间隔、wall 固定 30ms，证明总时长边界独立于活动边界
async def test_active_never_ending_stream_fails_with_wall_timeout() -> None:
    loop = AgentLoop(
        _ActiveForeverProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        watchdog=StreamWatchdog(
            WatchdogLimits(
                idle_timeout_s=0.02,
                wall_timeout_s=0.03,
                max_response_bytes=1_000_000,
            )
        ),
    )
    context = _context()

    await loop.run(context)

    assert context.status == "failed"
    assert context.reason == "stream_wall_timeout"


# 功能：验证非流式大响应也受 max response bytes 约束
# 设计：Provider 不发布 token 而直接返回 10KB 文本，覆盖完成响应的二次大小校验
async def test_non_stream_response_fails_when_too_large() -> None:
    loop = AgentLoop(
        _LargeResponseProvider(),  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        watchdog=StreamWatchdog(
            WatchdogLimits(
                idle_timeout_s=0.1,
                wall_timeout_s=1.0,
                max_response_bytes=512,
            )
        ),
    )
    context = _context()

    await loop.run(context)

    assert context.status == "failed"
    assert context.reason == "response_too_large"


# 功能：验证 transient 与 no-content 使用独立计数器并产生不同 retry 事件
# 设计：按 429 异常、空响应、成功响应排列，确认两类 attempt 都从一开始且最终成功
async def test_transient_and_no_content_retries_are_counted_separately() -> None:
    provider = _SequenceProvider(
        [
            RuntimeError("429 rate limit"),
            LlmResponse(stop_reason="end_turn"),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集结构化 retry 事件以验证类型和各自 attempt
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        bus,
        retry_backoff_s=0,
    )
    context = _context()

    await loop.run(context)

    retries = [event for event in events if event.type == "llm.retry"]  # type: ignore[attr-defined]
    assert context.status == "success"
    assert provider.calls == 3
    assert [event.kind for event in retries] == ["transient", "no_content"]  # type: ignore[attr-defined]
    assert [event.attempt for event in retries] == [1, 1]  # type: ignore[attr-defined]


# 功能：验证 Provider 首次流空闲超时会在同一步透明重试一次并保留结构化证据
# 设计：让伪 Provider 先直接抛 watchdog 异常再成功，避免等待真实时钟并精确断言调用次数
async def test_stream_idle_timeout_retries_once_and_recovers_turn() -> None:
    provider = _SequenceProvider(
        [
            StreamIdleTimeoutError("temporary idle stream"),
            LlmResponse(stop_reason="end_turn", text="recovered"),
        ]
    )
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集 retry 事件以确认超时没有被静默吞掉
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        bus,
        retry_backoff_s=0,
    )
    context = _context()

    await loop.run(context)

    retries = [event for event in events if event.type == "llm.retry"]  # type: ignore[attr-defined]
    assert context.status == "success"
    assert provider.calls == 2
    assert [event.kind for event in retries] == ["transient"]  # type: ignore[attr-defined]
    assert [event.attempt for event in retries] == [1]  # type: ignore[attr-defined]


# 功能：验证空响应重试耗尽后以 no_content 明确失败而不是伪装成功
# 设计：连续提供三次空 end_turn，覆盖初次调用加两次独立 no-content retry 的边界
async def test_no_content_retry_exhaustion_fails_explicitly() -> None:
    provider = _SequenceProvider([LlmResponse(stop_reason="end_turn") for _ in range(3)])
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        retry_backoff_s=0,
    )
    context = _context()

    await loop.run(context)

    assert provider.calls == 3
    assert context.status == "failed"
    assert context.reason == "no_content"

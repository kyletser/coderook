from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import cast

from pydantic import BaseModel

from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse


class StreamWatchdogError(RuntimeError):
    reason = "stream_watchdog"


class StreamIdleTimeoutError(StreamWatchdogError):
    reason = "stream_idle_timeout"


class StreamWallTimeoutError(StreamWatchdogError):
    reason = "stream_wall_timeout"


class ResponseTooLargeError(StreamWatchdogError):
    reason = "response_too_large"


class NoContentResponseError(RuntimeError):
    reason = "no_content"


@dataclass(frozen=True)
class WatchdogLimits:
    idle_timeout_s: float = 60.0
    wall_timeout_s: float = 180.0
    max_response_bytes: int = 8 * 1024 * 1024

    # 拒绝关闭或互相矛盾的 watchdog 边界
    def __post_init__(self) -> None:
        if self.idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s must be positive")
        if self.wall_timeout_s < self.idle_timeout_s:
            raise ValueError("wall_timeout_s must be at least idle_timeout_s")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")


class _ActivityBus(EventBus):
    # 包装真实 EventBus，在转发流式正文前更新活动时间和累计字节数
    def __init__(
        self,
        inner: EventBus,
        on_activity: Callable[[int], None],
    ) -> None:
        super().__init__()
        self._inner = inner
        self._on_activity = on_activity

    # 只把 token/reasoning 视为响应活动，其余生命周期事件不掩盖空闲流
    async def publish(self, event: BaseModel) -> None:
        event_type = str(getattr(event, "type", ""))
        if event_type == "llm.token":
            self._on_activity(len(str(getattr(event, "token", "")).encode("utf-8")))
        elif event_type == "llm.reasoning":
            self._on_activity(len(str(getattr(event, "content", "")).encode("utf-8")))
        await self._inner.publish(event)


class StreamWatchdog:
    # 初始化流式空闲、总时长与响应大小三重边界
    def __init__(self, limits: WatchdogLimits | None = None) -> None:
        self._limits = limits or WatchdogLimits()

    # 返回 LLMResponse 的稳定 JSON 字节大小以覆盖非流式和工具调用正文
    @staticmethod
    def _response_size(response: LlmResponse) -> int:
        return len(
            json.dumps(
                asdict(response),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )

    # 在活动与总时长边界内执行 provider 调用，并在退出时清理悬挂任务
    async def run(
        self,
        call: Callable[[EventBus], Awaitable[LlmResponse]],
        bus: EventBus,
    ) -> LlmResponse:
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_activity = started_at
        streamed_bytes = 0
        activity = asyncio.Event()

        # 更新最近流活动并在单次响应超过字节上限时立即中止 provider
        def _touch(size: int) -> None:
            nonlocal last_activity, streamed_bytes
            last_activity = loop.time()
            streamed_bytes += size
            if streamed_bytes > self._limits.max_response_bytes:
                raise ResponseTooLargeError(
                    "stream response exceeded "
                    f"{self._limits.max_response_bytes} bytes"
                )
            activity.set()

        # 把任意 Awaitable 归一为 Task，便于统一取消和获取类型化结果
        async def _run_call() -> LlmResponse:
            return await call(_ActivityBus(bus, _touch))

        task = asyncio.create_task(_run_call())
        try:
            while True:
                now = loop.time()
                wall_remaining = self._limits.wall_timeout_s - (now - started_at)
                idle_remaining = self._limits.idle_timeout_s - (now - last_activity)
                if wall_remaining <= 0:
                    raise StreamWallTimeoutError(
                        f"stream exceeded {self._limits.wall_timeout_s:g}s wall timeout"
                    )
                if idle_remaining <= 0:
                    raise StreamIdleTimeoutError(
                        f"stream was idle for {self._limits.idle_timeout_s:g}s"
                    )
                activity.clear()
                activity_waiter = asyncio.create_task(activity.wait())
                wait_set: set[asyncio.Future[object]] = {
                    cast(asyncio.Future[object], task),
                    cast(asyncio.Future[object], activity_waiter),
                }
                done, _pending = await asyncio.wait(
                    wait_set,
                    timeout=min(wall_remaining, idle_remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    activity_waiter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await activity_waiter
                    response = task.result()
                    size = self._response_size(response)
                    if size > self._limits.max_response_bytes:
                        raise ResponseTooLargeError(
                            "response exceeded "
                            f"{self._limits.max_response_bytes} bytes"
                        )
                    return response
                if activity_waiter in done:
                    await activity_waiter
                    continue
                activity_waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await activity_waiter
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

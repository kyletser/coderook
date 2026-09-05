from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)

type EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    # 初始化有序订阅列表及需要向发布方传播失败的关键处理器集合
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []
        self._critical: set[EventHandler] = set()

    # 注册事件处理函数，并允许持久化等边界处理器优先执行
    def subscribe(
        self,
        handler: EventHandler,
        *,
        first: bool = False,
        critical: bool = False,
    ) -> None:
        if first:
            self._subscribers.insert(0, handler)
        else:
            self._subscribers.append(handler)
        if critical:
            self._critical.add(handler)

    # 注销事件处理函数，避免短生命周期订阅者在总线上持续累积
    def unsubscribe(self, handler: EventHandler) -> None:
        self._critical.discard(handler)
        try:
            self._subscribers.remove(handler)
        except ValueError:
            return

    # 接收 Provider 已收到但尚不适合发布为用户事件的流活动，普通总线无需处理
    def mark_stream_activity(self, byte_count: int) -> None:
        del byte_count

    # 按注册顺序依次调用所有订阅者；单个订阅者异常被隔离，不中断后续订阅者
    async def publish(self, event: BaseModel) -> None:
        for handler in tuple(self._subscribers):
            try:
                await handler(event)
            except asyncio.CancelledError:
                # 保留取消语义，向上传播以正确终止 run
                raise
            except Exception:
                if handler in self._critical:
                    raise
                logger.exception("event subscriber failed on %s", type(event).__name__)

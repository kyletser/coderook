from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

logger = logging.getLogger(__name__)

type EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    # 注册事件处理函数，并允许持久化等边界处理器优先执行
    def subscribe(self, handler: EventHandler, *, first: bool = False) -> None:
        if first:
            self._subscribers.insert(0, handler)
        else:
            self._subscribers.append(handler)

    # 注销事件处理函数，避免短生命周期订阅者在总线上持续累积
    def unsubscribe(self, handler: EventHandler) -> None:
        try:
            self._subscribers.remove(handler)
        except ValueError:
            return

    # 按注册顺序依次调用所有订阅者；单个订阅者异常被隔离，不中断后续订阅者
    async def publish(self, event: BaseModel) -> None:
        for handler in tuple(self._subscribers):
            try:
                await handler(event)
            except asyncio.CancelledError:
                # 保留取消语义，向上传播以正确终止 run
                raise
            except Exception:
                logger.exception(
                    "event subscriber failed on %s", type(event).__name__
                )

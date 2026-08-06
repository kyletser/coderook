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

    # 注册一个事件处理函数
    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    # 按注册顺序依次调用所有订阅者；单个订阅者异常被隔离，不中断后续订阅者
    async def publish(self, event: BaseModel) -> None:
        for handler in self._subscribers:
            try:
                await handler(event)
            except asyncio.CancelledError:
                # 保留取消语义，向上传播以正确终止 run
                raise
            except Exception:
                logger.exception(
                    "event subscriber failed on %s", type(event).__name__
                )

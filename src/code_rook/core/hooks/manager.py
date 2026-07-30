from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

HookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
HookCallback = Callable[[dict[str, Any]], Awaitable["HookDecision | None"]]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookDecision:
    blocked: bool = False
    reason: str = ""


class HookManager:
    # 初始化按生命周期事件分组的异步回调表
    def __init__(self) -> None:
        self._callbacks: dict[HookEvent, list[HookCallback]] = {
            "UserPromptSubmit": [],
            "PreToolUse": [],
            "PostToolUse": [],
            "Stop": [],
        }

    # 注册一个异步 hook，并保持注册顺序执行
    def register(self, event: HookEvent, callback: HookCallback) -> None:
        self._callbacks[event].append(callback)

    # 触发生命周期事件；回调异常被隔离，首个阻断决定立即返回
    async def emit(self, event: HookEvent, context: dict[str, Any]) -> HookDecision:
        for callback in tuple(self._callbacks[event]):
            try:
                decision = await callback(context)
            except Exception:
                logger.exception("hook failed event=%s callback=%r", event, callback)
                continue
            if decision is not None and decision.blocked:
                return decision
        return HookDecision()

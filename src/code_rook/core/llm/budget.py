from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_OUTPUT_TOKEN_LIMIT: ContextVar[int | None] = ContextVar(
    "coderook_goal_output_token_limit",
    default=None,
)


# 在当前异步任务内设置单次模型输出上限，并在调用结束后恢复旧值
@contextmanager
def output_token_budget(limit: int) -> Iterator[None]:
    inherited = _OUTPUT_TOKEN_LIMIT.get()
    effective = max(1, limit) if inherited is None else min(inherited, max(1, limit))
    token = _OUTPUT_TOKEN_LIMIT.set(effective)
    try:
        yield
    finally:
        _OUTPUT_TOKEN_LIMIT.reset(token)


# 将 provider 默认输出上限收窄到当前调用允许值
def clamp_output_token_limit(default: int) -> int:
    limit = _OUTPUT_TOKEN_LIMIT.get()
    return default if limit is None else max(1, min(default, limit))

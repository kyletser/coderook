from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


# 解析服务端 Retry-After 秒数或 HTTP 日期，忽略无效值
def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 5
    initial_delay_s: float = 0.5
    max_delay_s: float = 10.0
    jitter_ratio: float = 0.1

    # 验证显式重试策略，零次重试允许关闭自动恢复
    def __post_init__(self) -> None:
        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ValueError("max_retries must be an integer")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not all(math.isfinite(v) for v in (
            self.initial_delay_s, self.max_delay_s, self.jitter_ratio,
        )) or not 0 <= self.initial_delay_s <= self.max_delay_s:
            raise ValueError("retry delays must be finite and ordered")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between zero and one")

    # 按同一步共享次数计算退避；服务端要求超过正常等待上限时不提前重试
    def delay(self, retry_number: int, retry_after_s: float | None = None) -> float | None:
        if retry_after_s is not None:
            return retry_after_s if retry_after_s <= self.max_delay_s else None
        base = min(self.max_delay_s, self.initial_delay_s * 2.0 ** min(retry_number - 1, 30))
        return min(self.max_delay_s, base * random.uniform(
            1 - self.jitter_ratio, 1 + self.jitter_ratio,
        ))

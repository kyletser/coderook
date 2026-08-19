import time
from typing import Any


class Cache:
    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._values: dict[str, tuple[float, Any]] = {}

    # 写入值并记录不受系统时钟回拨影响的时间戳
    def put(self, key: str, value: Any) -> None:
        self._values[key] = (time.monotonic(), value)

    # 返回未过期值并惰性清理已过期条目
    def get(self, key: str) -> Any | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        created_at, value = entry
        if time.monotonic() - created_at >= self._ttl_s:
            self._values.pop(key, None)
            return None
        return value

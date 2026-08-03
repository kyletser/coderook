from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass

from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import ToolResult


@dataclass(frozen=True)
class StuckMatch:
    tool_name: str
    signature: str
    repeat_count: int


class StuckGuard:
    # 初始化最近语义步骤窗口与连续重复阈值
    def __init__(self, *, threshold: int = 3, window: int = 12) -> None:
        if threshold < 2:
            raise ValueError("threshold must be at least two")
        if window < threshold:
            raise ValueError("window must be at least threshold")
        self._threshold = threshold
        self._history: deque[str] = deque(maxlen=window)

    # 对工具名、规范化参数和结果 hash，避免把敏感正文写入事件
    @staticmethod
    def _signature(tool_call: ToolCallBlock, result: ToolResult) -> str:
        payload = json.dumps(
            {
                "tool": tool_call.name,
                "input": tool_call.input,
                "result": result.content,
                "is_error": result.is_error,
                "error_type": result.error_type,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # 记录一次语义工具步骤，仅在连续重复首次达到阈值时返回命中证据
    def observe(
        self,
        tool_call: ToolCallBlock,
        result: ToolResult,
    ) -> StuckMatch | None:
        signature = self._signature(tool_call, result)
        self._history.append(signature)
        repeat_count = 0
        for item in reversed(self._history):
            if item != signature:
                break
            repeat_count += 1
        if repeat_count != self._threshold:
            return None
        return StuckMatch(
            tool_name=tool_call.name,
            signature=signature,
            repeat_count=repeat_count,
        )

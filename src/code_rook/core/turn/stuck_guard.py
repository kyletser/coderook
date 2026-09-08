from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fnmatch import fnmatchcase

from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import ToolResult


@dataclass(frozen=True)
class StuckMatch:
    tool_name: str
    signature: str
    repeat_count: int
    notice: str


class StuckGuard:
    # 只观察同一 Agent 的连续同参调用，默认在第三、五、八次给出建议
    def __init__(
        self, *, thresholds: tuple[int, ...] = (3, 5, 8),
        include: tuple[str, ...] = (), exclude: tuple[str, ...] = (),
        argument_preview_chars: int = 500,
    ) -> None:
        if not thresholds or any(n < 2 for n in thresholds):
            raise ValueError("repeat thresholds must be at least two")
        if argument_preview_chars < 0:
            raise ValueError("argument_preview_chars must not be negative")
        self._thresholds = sorted(set(thresholds))
        self._include = include
        self._exclude = exclude
        self._preview_chars = argument_preview_chars
        self.reset()

    # 用户新消息使旧重复链失效，系统提醒不调用此入口
    def reset(self) -> None:
        self._last_signature = ""
        self._count = 0

    # 参数递归排序后比较，工具结果和调用 ID 不参与重复判断
    def observe(
        self,
        tool_call: ToolCallBlock,
        result: ToolResult,
    ) -> StuckMatch | None:
        del result
        name = tool_call.name
        if (self._include and not any(fnmatchcase(name, p) for p in self._include)) or any(
            fnmatchcase(name, p) for p in self._exclude
        ):
            return None
        arguments = json.dumps(tool_call.input, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"))
        signature = hashlib.sha256((name + "\n" + arguments).encode()).hexdigest()
        self._count = self._count + 1 if signature == self._last_signature else 1
        self._last_signature = signature
        if self._count not in self._thresholds:
            return None
        notice = (
            f"Repeat-tool reminder: {name} has been called {self._count} consecutive times "
            "with identical arguments. Review the previous results and consider a different "
            "approach, or finish if the task is complete. This is advisory; you may continue "
            "the same call when it is useful."
        )
        if self._count != self._thresholds[0] and self._preview_chars:
            notice += "\nArguments: " + arguments[:self._preview_chars]
            if len(arguments) > self._preview_chars:
                notice += "…"
        return StuckMatch(name, signature, self._count, notice)

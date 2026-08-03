from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ToolCapability, ToolCatalogError


class ReadRepeatGuard:
    # 初始化当前 turn loop 使用的有界只读结果 LRU
    def __init__(self, *, max_entries: int = 64) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._cache: OrderedDict[str, ToolResult] = OrderedDict()

    # 为纯读工具生成稳定调用键，副作用、未知或声明不完整的工具返回空
    def call_key(
        self,
        registry: ToolRegistry,
        tool_call: ToolCallBlock,
    ) -> str | None:
        try:
            resolved = registry.resolve_call(tool_call.name, dict(tool_call.input))
        except (ToolCatalogError, PermissionError, ValueError, OSError):
            return None
        if resolved.action.is_mutating:
            return None
        if ToolCapability.READ not in resolved.action.capabilities:
            return None
        if not {"path", "sha256", "handle"} & tool_call.input.keys():
            return None
        payload = json.dumps(
            {"tool": tool_call.name, "input": tool_call.input},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    # 返回缓存结果副本并更新 LRU 顺序
    def get(self, key: str) -> ToolResult | None:
        result = self._cache.get(key)
        if result is None:
            return None
        self._cache.move_to_end(key)
        return ToolResult(
            result.content,
            is_error=result.is_error,
            error_type=result.error_type,
        )

    # 保存成功只读结果并驱逐最旧项
    def put(self, key: str, result: ToolResult) -> None:
        if result.is_error:
            return
        self._cache[key] = ToolResult(result.content)
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    # 在任意 mutation 后清空缓存，避免向模型返回修改前的陈旧读取
    def clear(self) -> None:
        self._cache.clear()

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from code_rook.core.tools.base import BaseTool, ToolResult
    from code_rook.core.tools.invocation import invoke_tool
    from code_rook.core.tools.registry import ToolRegistry
    from code_rook.core.tools.spec import ToolCapability, ToolSpec

__all__ = [
    "BaseTool",
    "ToolCapability",
    "ToolResult",
    "ToolRegistry",
    "ToolSpec",
    "invoke_tool",
]

_EXPORTS = {
    "BaseTool": ("code_rook.core.tools.base", "BaseTool"),
    "ToolResult": ("code_rook.core.tools.base", "ToolResult"),
    "invoke_tool": ("code_rook.core.tools.invocation", "invoke_tool"),
    "ToolRegistry": ("code_rook.core.tools.registry", "ToolRegistry"),
    "ToolCapability": ("code_rook.core.tools.spec", "ToolCapability"),
    "ToolSpec": ("code_rook.core.tools.spec", "ToolSpec"),
}


# 按需解析兼容导出，避免导入基础工具类型时提前装载完整调用管线。
def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value

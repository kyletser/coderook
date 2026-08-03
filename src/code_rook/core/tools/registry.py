from __future__ import annotations

from code_rook.core.authority import RuntimeMode
from code_rook.core.tools.base import BaseTool
from code_rook.core.tools.catalog import ToolCatalog
from code_rook.core.tools.spec import (
    ResolvedToolCall,
    ResourceClaim,
    ToolCaller,
    ToolCatalogError,
    ToolSpec,
)


class ToolRegistry:
    # 初始化工具实现表和同一事实来源的稳定目录
    def __init__(self, runtime_mode: RuntimeMode = RuntimeMode.ACT) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._catalog = ToolCatalog()
        self._runtime_mode = runtime_mode

    # 注册工具；同名覆盖
    def register(self, tool: BaseTool, *, spec: ToolSpec | None = None) -> None:
        self._tools[tool.name] = tool
        resolved_spec = spec or tool.build_spec()
        if resolved_spec.name != tool.name:
            raise ValueError("tool implementation and ToolSpec names must match")
        self._catalog.register(resolved_spec)

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    # 返回按名称稳定排序且按当前 Mode 裁剪的模型 schema
    def tool_schemas(self, *, activated: tuple[str, ...] = ()) -> list[dict[str, object]]:
        return self._catalog.tool_schemas(self._runtime_mode, activated=activated)

    # 返回模型目录的 canonical JSON 字节并复用 memoized 结果
    def canonical_catalog_json(self, *, activated: tuple[str, ...] = ()) -> bytes:
        return self._catalog.canonical_json(self._runtime_mode, activated=activated)

    # 返回 always-active schema 头部的稳定指纹
    def active_head_hash(self) -> str:
        return self._catalog.active_head_hash(self._runtime_mode)

    # 校验工具 caller/action 并返回解析后的 V2 调用声明
    def resolve_call(
        self,
        name: str,
        params: dict[str, object],
        *,
        caller: ToolCaller | str = ToolCaller.MODEL,
    ) -> ResolvedToolCall:
        resolved = self._catalog.resolve_call(name, params, caller=caller)
        if resolved.action not in resolved.spec.visible_actions(self._runtime_mode):
            raise ToolCatalogError(
                f"action {resolved.action.name} is unavailable in {self._runtime_mode.value} mode"
            )
        return resolved

    # 校验调用后返回工具声明的资源占用集合
    def resource_claims(
        self,
        name: str,
        params: dict[str, object],
        *,
        caller: ToolCaller | str = ToolCaller.MODEL,
    ) -> tuple[ResourceClaim, ...]:
        self.resolve_call(name, params, caller=caller)
        tool = self._tools[name]
        return tool.resource_claims(params)

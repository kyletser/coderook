from __future__ import annotations

from typing import TYPE_CHECKING

from code_rook.core.authority import RuntimeMode, ToolAction
from code_rook.core.tools.base import BaseTool
from code_rook.core.tools.catalog import ToolCatalog
from code_rook.core.tools.spec import (
    ResolvedToolCall,
    ResourceClaim,
    ToolCaller,
    ToolCatalogError,
    ToolSpec,
)

if TYPE_CHECKING:
    from code_rook.core.tools.discovery import ToolSearchMatch

DEFAULT_MODEL_TOOL_LIMIT = 32


class ToolRegistry:
    # 初始化工具实现表和同一事实来源的稳定目录
    def __init__(
        self,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        *,
        model_tool_limit: int = DEFAULT_MODEL_TOOL_LIMIT,
        allowed_authority_actions: frozenset[ToolAction] | None = None,
    ) -> None:
        if model_tool_limit < 1:
            raise ValueError("model_tool_limit must be positive")
        self._tools: dict[str, BaseTool] = {}
        self._catalog = ToolCatalog()
        self._runtime_mode = runtime_mode
        self._activated_deferred: list[str] = []
        self._model_tool_limit = model_tool_limit
        self._allowed_authority_actions = allowed_authority_actions

    # 按不可变 authority ceiling 裁剪 action，目录和直接调用共同 fail closed
    def _authority_filtered_spec(self, spec: ToolSpec) -> ToolSpec | None:
        if self._allowed_authority_actions is None:
            return spec
        actions = tuple(
            action
            for action in spec.actions
            if action.authority_action() in self._allowed_authority_actions
        )
        if not actions:
            return None
        capabilities = frozenset(
            capability
            for action in actions
            for capability in action.capabilities
        )
        return spec.model_copy(
            update={"actions": actions, "capabilities": capabilities}
        )

    # 注册工具；同名覆盖
    def register(self, tool: BaseTool, *, spec: ToolSpec | None = None) -> None:
        resolved_spec = spec or tool.build_spec()
        if resolved_spec.name != tool.name:
            raise ValueError("tool implementation and ToolSpec names must match")
        filtered_spec = self._authority_filtered_spec(resolved_spec)
        if filtered_spec is None:
            return
        self._tools[tool.name] = tool
        self._catalog.register(filtered_spec)

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    # 返回按名称稳定排序且按当前 Mode 裁剪的模型 schema
    def tool_schemas(self, *, activated: tuple[str, ...] = ()) -> list[dict[str, object]]:
        combined = tuple(dict.fromkeys((*self._activated_deferred, *activated)))
        schemas = self._catalog.tool_schemas(self._runtime_mode, activated=combined)
        if len(schemas) > self._model_tool_limit:
            raise ToolCatalogError(
                "model-visible tool limit exceeded: "
                f"{len(schemas)} > {self._model_tool_limit}"
            )
        return schemas

    # 返回模型目录的 canonical JSON 字节并复用 memoized 结果
    def canonical_catalog_json(self, *, activated: tuple[str, ...] = ()) -> bytes:
        combined = tuple(dict.fromkeys((*self._activated_deferred, *activated)))
        self.tool_schemas(activated=activated)
        return self._catalog.canonical_json(self._runtime_mode, activated=combined)

    # 返回模型目录允许暴露的工具数量硬上限
    @property
    def model_tool_limit(self) -> int:
        return self._model_tool_limit

    # 返回 always-active schema 头部的稳定指纹
    def active_head_hash(self) -> str:
        return self._catalog.active_head_hash(self._runtime_mode)

    # 返回全部模型可发现的 deferred 工具声明
    def deferred_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            spec
            for spec in self._catalog.specs()
            if (
                spec.deferred
                and spec.model_visible
                and spec.visible_actions(self._runtime_mode)
            )
        )

    # 确定性搜索 deferred 工具并把命中项追加到激活尾部
    def search_deferred(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> tuple[ToolSearchMatch, ...]:
        from code_rook.core.tools.discovery import search_deferred_specs

        matches = search_deferred_specs(self.deferred_specs(), query, limit=limit)
        remaining = self._model_tool_limit - len(self.tool_schemas())
        selected: list[ToolSearchMatch] = []
        for match in matches:
            if match.name in self._activated_deferred:
                selected.append(match)
                continue
            if remaining <= 0:
                continue
            self._activated_deferred.append(match.name)
            remaining -= 1
            selected.append(match)
        return tuple(selected)

    # 校验工具 caller/action 并返回解析后的 V2 调用声明
    def resolve_call(
        self,
        name: str,
        params: dict[str, object],
        *,
        caller: ToolCaller | str = ToolCaller.MODEL,
    ) -> ResolvedToolCall:
        resolved = self._catalog.resolve_call(name, params, caller=caller)
        if (
            resolved.caller == ToolCaller.MODEL
            and resolved.spec.deferred
            and name not in self._activated_deferred
        ):
            raise ToolCatalogError(f"deferred tool is not activated: {name}")
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

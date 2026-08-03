from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ResourceClaim,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolSpec,
)
from code_rook.core.workspace import WorkspaceBoundary

_ACTION_ALIASES = {
    "read": "read_file",
    "list": "list_dir",
    "search_name": "glob",
    "search_content": "grep",
    "write": "write_file",
    "edit": "edit_file",
    "patch": "apply_patch",
}

_ACTION_CAPABILITIES = {
    "read": frozenset({ToolCapability.READ}),
    "list": frozenset({ToolCapability.READ}),
    "search_name": frozenset({ToolCapability.READ}),
    "search_content": frozenset({ToolCapability.READ}),
    "write": frozenset({ToolCapability.WRITE}),
    "edit": frozenset({ToolCapability.WRITE}),
    "patch": frozenset({ToolCapability.WRITE}),
}


class FileTool(BaseTool):
    name = "File"
    description = (
        "Read, list, search, write, edit, or patch files inside the workspace. "
        "Choose one action and provide the parameters required by that action."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTION_ALIASES),
            }
        },
        "required": ["action"],
    }
    side_effect = ToolSideEffect.LOCAL_WRITE

    # 初始化 File family，并按传入 legacy backend 集合限制可用 action
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        backends: Mapping[str, BaseTool],
    ) -> None:
        self._boundary = boundary
        self._backends = {
            action: backends[alias]
            for action, alias in _ACTION_ALIASES.items()
            if alias in backends
        }
        if not self._backends:
            raise ValueError("File family requires at least one backend")

    # 返回当前 family action 对应的旧工具名，供 whitelist 和 replay 迁移使用
    @property
    def legacy_aliases(self) -> frozenset[str]:
        return frozenset(_ACTION_ALIASES[action] for action in self._backends)

    # 从实际 backend 生成 action 级 schema 和 capability
    def build_spec(self) -> ToolSpec:
        actions = tuple(
            ToolActionSpec(
                name=action,
                description=backend.description,
                input_schema=backend.input_schema,
                capabilities=_ACTION_CAPABILITIES[action],
                approval_requirement=(
                    ApprovalRequirement.NEVER
                    if _ACTION_CAPABILITIES[action] == frozenset({ToolCapability.READ})
                    else ApprovalRequirement.POLICY
                ),
                parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
            )
            for action, backend in self._backends.items()
        )
        capabilities = frozenset(
            capability
            for action in actions
            for capability in action.capabilities
        )
        return ToolSpec(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=self.input_schema,
            actions=actions,
            capabilities=capabilities,
            approval_requirement=ApprovalRequirement.POLICY,
            parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
            allowed_callers=frozenset({ToolCaller.MODEL, ToolCaller.INTERNAL}),
        )

    # 将 action 调用分派到原有文件工具并保持其执行语义
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        action = params.get("action")
        if not isinstance(action, str) or action not in self._backends:
            return ToolResult(
                content=f"unknown File action: {action}",
                is_error=True,
                error_type="schema_error",
            )
        backend_params = dict(params)
        backend_params.pop("action", None)
        try:
            return await self._backends[action].invoke(backend_params)
        except ValidationError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_type="schema_error",
            )

    # 按 action 声明共享读取或独占写入资源
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...]:
        action = str(params.get("action", ""))
        if action not in self._backends:
            return ()
        capability = next(iter(_ACTION_CAPABILITIES[action]))
        if action == "patch":
            return (
                ResourceClaim(
                    resource="workspace:/**",
                    capability=capability,
                    exclusive=True,
                ),
            )
        path = str(params.get("path", "."))
        resolved = self._boundary.resolve(path)
        relative = resolved.relative_to(self._boundary.root).as_posix() or "."
        return (
            ResourceClaim(
                resource=f"workspace:{relative}",
                capability=capability,
                exclusive=capability == ToolCapability.WRITE,
            ),
        )


# 注册 File family，并把允许的旧工具降级为 internal/replay alias
def register_file_family(
    registry: ToolRegistry,
    boundary: WorkspaceBoundary,
    tools: list[BaseTool],
    *,
    allowed_names: set[str] | None = None,
) -> FileTool | None:
    backends = {
        tool.name: tool
        for tool in tools
        if allowed_names is None
        or "File" in allowed_names
        or tool.name in allowed_names
    }
    for tool in tools:
        if allowed_names is not None and tool.name not in allowed_names:
            continue
        legacy_spec = tool.build_spec().model_copy(
            update={
                "model_visible": False,
                "allowed_callers": frozenset(
                    {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                ),
            }
        )
        registry.register(tool, spec=legacy_spec)
    if not backends:
        return None
    family = FileTool(boundary, backends)
    registry.register(family)
    return family

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_rook.core.bus.events import PlanStepState, PlanUpdatedEvent
from code_rook.core.events.bus import EventBus
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

_MEMORY_ACTION_ALIASES = {
    "save": "memory_save",
    "search": "memory_search",
    "forget": "memory_forget",
}
_TASK_ACTION_ALIASES = {
    "create": "task_create",
    "claim": "task_claim",
    "update": "task_update",
    "list": "task_list",
    "get": "task_get",
}
_MEMORY_ACTION_CAPABILITIES = {
    "save": frozenset({ToolCapability.WRITE}),
    "search": frozenset({ToolCapability.READ}),
    "forget": frozenset({ToolCapability.WRITE}),
}
_TASK_ACTION_CAPABILITIES = {
    "create": frozenset({ToolCapability.WRITE}),
    "claim": frozenset({ToolCapability.WRITE}),
    "update": frozenset({ToolCapability.WRITE}),
    "list": frozenset({ToolCapability.READ}),
    "get": frozenset({ToolCapability.READ}),
}


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 为 action-family 构建完整 action schema 与 capability 并集
def _family_spec(
    *,
    name: str,
    description: str,
    backends: Mapping[str, BaseTool],
    capabilities: Mapping[str, frozenset[ToolCapability]],
) -> ToolSpec:
    actions = tuple(
        ToolActionSpec(
            name=action,
            description=backend.description,
            input_schema=backend.input_schema,
            capabilities=capabilities[action],
            approval_requirement=(
                ApprovalRequirement.NEVER
                if capabilities[action] == frozenset({ToolCapability.READ})
                else ApprovalRequirement.POLICY
            ),
            parallel_policy=(
                ParallelPolicy.SAFE
                if capabilities[action] == frozenset({ToolCapability.READ})
                else ParallelPolicy.RESOURCE_CLAIMS
            ),
        )
        for action, backend in backends.items()
    )
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
        actions=actions,
        capabilities=frozenset(
            capability for action in actions for capability in action.capabilities
        ),
        approval_requirement=ApprovalRequirement.POLICY,
        parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
    )


# 将 family action 分派给隐藏的兼容 backend 并归一化参数错误
async def _invoke_backend(
    family_name: str,
    backends: Mapping[str, BaseTool],
    params: dict[str, object],
) -> ToolResult:
    action = params.get("action")
    if not isinstance(action, str) or action not in backends:
        return ToolResult(
            f"unknown {family_name} action: {action}",
            is_error=True,
            error_type="schema_error",
        )
    payload = dict(params)
    payload.pop("action", None)
    try:
        return await backends[action].invoke(payload)
    except (KeyError, TypeError, ValidationError) as exc:
        return ToolResult(str(exc), is_error=True, error_type="schema_error")


# 注册 legacy backend 为仅供 internal/replay 使用的隐藏工具
def _register_legacy_aliases(
    registry: ToolRegistry,
    backends: Mapping[str, BaseTool],
    *,
    allowed_names: set[str] | None,
) -> None:
    for backend in backends.values():
        if allowed_names is not None and backend.name not in allowed_names:
            continue
        registry.register(
            backend,
            spec=backend.build_spec().model_copy(
                update={
                    "model_visible": False,
                    "allowed_callers": frozenset(
                        {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                    ),
                }
            ),
        )


class MemoryTool(BaseTool):
    name = "memory"
    description = "Save, search, or forget durable project memories."
    side_effect = ToolSideEffect.LOCAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 绑定当前可用的 memory action backend
    def __init__(self, backends: Mapping[str, BaseTool]) -> None:
        self._backends = dict(backends)

    # 生成按 action 区分读写能力的 Memory ToolSpec
    def build_spec(self) -> ToolSpec:
        return _family_spec(
            name=self.name,
            description=self.description,
            backends=self._backends,
            capabilities=_MEMORY_ACTION_CAPABILITIES,
        )

    # 调用对应 memory backend
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return await _invoke_backend(self.name, self._backends, params)

    # 将 memory action 解析到隐藏 backend 并移除公开 action 字段
    def execution_target(
        self,
        params: dict[str, object],
    ) -> tuple[BaseTool, dict[str, object]]:
        action = params.get("action")
        if not isinstance(action, str) or action not in self._backends:
            return self, dict(params)
        payload = dict(params)
        payload.pop("action", None)
        return self._backends[action], payload

    # 对同一项目记忆库声明共享读或独占写
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...]:
        action = str(params.get("action", ""))
        capabilities = _MEMORY_ACTION_CAPABILITIES.get(action)
        if capabilities is None:
            return ()
        capability = next(iter(capabilities))
        return (
            ResourceClaim(
                resource="memory:project",
                capability=capability,
                exclusive=capability == ToolCapability.WRITE,
            ),
        )


class TasksTool(BaseTool):
    name = "tasks"
    description = (
        "Create, claim, update, list, or inspect durable CodeRook tasks for the current run. "
        "Its scope is only CodeRook task records, not project or host entities."
    )
    side_effect = ToolSideEffect.LOCAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 绑定当前 run 的 task action backend
    def __init__(self, backends: Mapping[str, BaseTool]) -> None:
        self._backends = dict(backends)

    # 生成按 action 区分读写能力的 Tasks ToolSpec
    def build_spec(self) -> ToolSpec:
        return _family_spec(
            name=self.name,
            description=self.description,
            backends=self._backends,
            capabilities=_TASK_ACTION_CAPABILITIES,
        )

    # 调用对应 task backend
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return await _invoke_backend(self.name, self._backends, params)

    # 将 tasks action 解析到隐藏 backend 并移除公开 action 字段
    def execution_target(
        self,
        params: dict[str, object],
    ) -> tuple[BaseTool, dict[str, object]]:
        action = params.get("action")
        if not isinstance(action, str) or action not in self._backends:
            return self, dict(params)
        payload = dict(params)
        payload.pop("action", None)
        return self._backends[action], payload

    # 对当前 run 的任务控制面声明共享读或独占写
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...]:
        action = str(params.get("action", ""))
        capabilities = _TASK_ACTION_CAPABILITIES.get(action)
        if capabilities is None:
            return ()
        capability = next(iter(capabilities))
        return (
            ResourceClaim(
                resource="tasks:run",
                capability=capability,
                exclusive=capability == ToolCapability.WRITE,
            ),
        )


class UpdatePlanParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    explanation: str = Field(default="", max_length=2_000)
    plan: list[PlanStepState] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    # 限制同一计划最多只有一个正在执行的步骤
    def validate_in_progress_count(self) -> UpdatePlanParams:
        if sum(step.status == "in_progress" for step in self.plan) > 1:
            raise ValueError("plan may contain at most one in_progress step")
        return self


class UpdatePlanTool(BaseTool):
    name = "update_plan"
    description = "Publish the current ordered implementation plan for user-visible progress."
    side_effect = ToolSideEffect.NONE
    params_model = UpdatePlanParams
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "explanation": {"type": "string", "maxLength": 2000},
            "plan": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["step", "status"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["plan"],
        "additionalProperties": False,
    }

    # 绑定当前 run 的事件总线
    def __init__(self, bus: EventBus, run_id: str) -> None:
        self._bus = bus
        self._run_id = run_id

    # 发布可由 runtime 持久投影的类型化计划事件
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = UpdatePlanParams.model_validate(params)
        await self._bus.publish(
            PlanUpdatedEvent(
                run_id=self._run_id,
                explanation=parsed.explanation,
                plan=parsed.plan,
                ts=_now(),
            )
        )
        completed = sum(step.status == "completed" for step in parsed.plan)
        return ToolResult(f"plan updated: {completed}/{len(parsed.plan)} completed")


# 注册 memory family 并隐藏旧平铺名称
def register_memory_family(
    registry: ToolRegistry,
    tools: list[BaseTool],
    *,
    allowed_names: set[str] | None = None,
) -> MemoryTool | None:
    by_alias = {tool.name: tool for tool in tools}
    backends = {
        action: by_alias[alias]
        for action, alias in _MEMORY_ACTION_ALIASES.items()
        if alias in by_alias
        and (
            allowed_names is None
            or "memory" in allowed_names
            or alias in allowed_names
        )
    }
    _register_legacy_aliases(registry, backends, allowed_names=allowed_names)
    if not backends:
        return None
    family = MemoryTool(backends)
    registry.register(family)
    return family


# 注册 tasks family 并隐藏旧平铺名称
def register_tasks_family(
    registry: ToolRegistry,
    tools: list[BaseTool],
    *,
    allowed_names: set[str] | None = None,
) -> TasksTool | None:
    by_alias = {tool.name: tool for tool in tools}
    backends = {
        action: by_alias[alias]
        for action, alias in _TASK_ACTION_ALIASES.items()
        if alias in by_alias
        and (
            allowed_names is None
            or "tasks" in allowed_names
            or alias in allowed_names
        )
    }
    _register_legacy_aliases(registry, backends, allowed_names=allowed_names)
    if not backends:
        return None
    family = TasksTool(backends)
    registry.register(family)
    return family

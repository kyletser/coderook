from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from code_rook.core.authority import RuntimeMode, ToolAction


class ToolCapability(StrEnum):
    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    NETWORK = "network"
    GIT = "git"
    EXTERNAL = "external"


class ApprovalRequirement(StrEnum):
    NEVER = "never"
    POLICY = "policy"
    ALWAYS = "always"


class ParallelPolicy(StrEnum):
    SERIAL = "serial"
    SAFE = "safe"
    RESOURCE_CLAIMS = "resource_claims"


class ToolCaller(StrEnum):
    MODEL = "model"
    INTERNAL = "internal"
    REPLAY = "replay"


class OutputPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    soft_limit: int = Field(default=20_000, ge=1)
    hard_limit: int = Field(default=100_000, ge=1)
    spill_to_artifact: bool = True

    @model_validator(mode="after")
    # 确保工具输出软限制不超过硬限制
    def validate_limits(self) -> OutputPolicy:
        if self.soft_limit > self.hard_limit:
            raise ValueError("soft_limit must not exceed hard_limit")
        return self


class ToolActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    description: str = ""
    input_schema: dict[str, object] | None = None
    capabilities: frozenset[ToolCapability] = Field(min_length=1)
    approval_requirement: ApprovalRequirement | None = None
    parallel_policy: ParallelPolicy | None = None

    # 返回该 action 是否会产生 mutation、进程、网络或外部副作用
    @property
    def is_mutating(self) -> bool:
        return bool(
            self.capabilities
            & {
                ToolCapability.WRITE,
                ToolCapability.PROCESS,
                ToolCapability.NETWORK,
                ToolCapability.EXTERNAL,
            }
        )

    # 将 action capability 收敛为现有 authority evaluator 使用的权限动作
    def authority_action(self) -> ToolAction:
        if ToolCapability.PROCESS in self.capabilities:
            return ToolAction.SHELL
        if ToolCapability.WRITE in self.capabilities:
            return ToolAction.MUTATE
        if self.capabilities & {ToolCapability.NETWORK, ToolCapability.EXTERNAL}:
            return ToolAction.EXTERNAL
        return ToolAction.READ


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    version: str = Field(default="1", min_length=1)
    description: str
    input_schema: dict[str, object]
    actions: tuple[ToolActionSpec, ...] = Field(min_length=1)
    capabilities: frozenset[ToolCapability] = Field(min_length=1)
    approval_requirement: ApprovalRequirement = ApprovalRequirement.POLICY
    parallel_policy: ParallelPolicy = ParallelPolicy.SERIAL
    allowed_callers: frozenset[ToolCaller] = Field(
        default_factory=lambda: frozenset({ToolCaller.MODEL, ToolCaller.INTERNAL})
    )
    model_visible: bool = True
    deferred: bool = False
    output_policy: OutputPolicy = Field(default_factory=OutputPolicy)

    @model_validator(mode="after")
    # 校验 action 唯一且顶层 capability 完整覆盖 action 声明
    def validate_actions(self) -> ToolSpec:
        names = [action.name for action in self.actions]
        if len(names) != len(set(names)):
            raise ValueError("tool action names must be unique")
        schema_count = sum(action.input_schema is not None for action in self.actions)
        if schema_count not in {0, len(self.actions)}:
            raise ValueError("tool actions must either all declare input_schema or all inherit")
        action_capabilities = frozenset(
            capability
            for action in self.actions
            for capability in action.capabilities
        )
        if action_capabilities != self.capabilities:
            raise ValueError("tool capabilities must equal the union of action capabilities")
        return self

    # 按名称解析 action，未知 action 直接返回 None
    def action(self, name: str) -> ToolActionSpec | None:
        return next((action for action in self.actions if action.name == name), None)

    # 返回指定 Mode 下允许暴露给模型的 action
    def visible_actions(self, mode: RuntimeMode) -> tuple[ToolActionSpec, ...]:
        if mode != RuntimeMode.PLAN:
            return self.actions
        return tuple(action for action in self.actions if not action.is_mutating)


class ResourceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource: str = Field(min_length=1)
    capability: ToolCapability
    exclusive: bool = False


class ResolvedToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: ToolSpec
    action: ToolActionSpec
    caller: ToolCaller

    # 返回 action override 或工具级默认审批要求
    @property
    def effective_approval_requirement(self) -> ApprovalRequirement:
        return self.action.approval_requirement or self.spec.approval_requirement

    # 返回 action override 或工具级默认并行策略
    @property
    def effective_parallel_policy(self) -> ParallelPolicy:
        return self.action.parallel_policy or self.spec.parallel_policy


class ToolCatalogError(ValueError):
    pass

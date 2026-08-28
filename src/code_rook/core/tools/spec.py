from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class ToolPresentationKind(StrEnum):
    GENERIC = "generic"
    TERMINAL = "terminal"
    DIFF = "diff"
    READ = "read"
    SEARCH = "search"
    WEB = "web"


class ToolPresentationAction(StrEnum):
    GENERIC = "generic"
    RUN_COMMAND = "run_command"
    RUN_TESTS = "run_tests"
    READ_FILE = "read_file"
    BROWSE_FILES = "browse_files"
    SEARCH_CODE = "search_code"
    EDIT_CODE = "edit_code"
    GIT = "git"
    WEB = "web"
    WORKER = "worker"


class ToolPresentationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ToolPresentationKind = ToolPresentationKind.GENERIC
    title_key: str = "tool.generic"
    subject_fields: tuple[str, ...] = ()
    location_fields: tuple[str, ...] = ()
    supports_live_output: bool = False
    result_schema_version: int = Field(default=1, ge=1)


_PERMISSION_KEY = re.compile(r"^[A-Za-z0-9_.:-]+$")


class PermissionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()

    @model_validator(mode="after")
    # 校验权限键及兼容别名唯一且仅包含可持久化的安全字符
    def validate_keys(self) -> PermissionScope:
        keys = (self.key, *self.aliases)
        if any(_PERMISSION_KEY.fullmatch(key) is None for key in keys):
            raise ValueError("permission keys may contain only letters, digits, ._:-")
        if len(keys) != len(set(keys)):
            raise ValueError("permission scope key and aliases must be unique")
        return self

    # 返回按新键优先、旧别名回退的稳定查找顺序
    @property
    def lookup_keys(self) -> tuple[str, ...]:
        return (self.key, *self.aliases)


class OutputPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # 默认先于上下文层 8K 截断落盘，保证任何摘要都带可恢复 Artifact 句柄
    soft_limit: int = Field(default=8_000, ge=1)
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
    permission_policy_key: str | None = None
    permission_policy_aliases: tuple[str, ...] = ()
    presentation: ToolPresentationSpec = Field(default_factory=ToolPresentationSpec)
    programmable: bool = True

    @field_validator("permission_policy_key")
    @classmethod
    # 校验显式权限键可安全写入 policy.toml 和缓存
    def validate_permission_policy_key(cls, value: str | None) -> str | None:
        if value is not None and _PERMISSION_KEY.fullmatch(value) is None:
            raise ValueError("permission policy key contains unsupported characters")
        return value

    @field_validator("permission_policy_aliases")
    @classmethod
    # 校验兼容策略别名不重复且不包含路径或空白字符
    def validate_permission_policy_aliases(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("permission policy aliases must be unique")
        if any(_PERMISSION_KEY.fullmatch(alias) is None for alias in value):
            raise ValueError("permission policy alias contains unsupported characters")
        return value

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

    # 从 action 声明生成精确缓存键及旧策略兼容别名
    def permission_scope(self, tool_name: str, *, is_family: bool) -> PermissionScope:
        default_key = f"{tool_name}.{self.name}" if is_family else tool_name
        return PermissionScope(
            key=self.permission_policy_key or default_key,
            aliases=self.permission_policy_aliases,
        )


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

    # 判断该工具是否要求显式 action 判别而不是单一 invoke 入口
    @property
    def is_action_family(self) -> bool:
        return not (len(self.actions) == 1 and self.actions[0].name == "invoke")

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
        permission_keys = [
            action.permission_scope(self.name, is_family=self.is_action_family).key
            for action in self.actions
        ]
        if len(permission_keys) != len(set(permission_keys)):
            raise ValueError("tool actions must have unique permission policy keys")
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

    # 返回 manifest 为当前调用声明的参数 schema
    @property
    def input_schema(self) -> dict[str, object]:
        return self.action.input_schema or self.spec.input_schema

    # 返回 manifest 为当前调用声明的不可变 capability 集合
    @property
    def capabilities(self) -> frozenset[ToolCapability]:
        return self.action.capabilities

    # 返回 manifest capability 推导出的 authority action
    @property
    def authority_action(self) -> ToolAction:
        return self.action.authority_action()

    # 返回当前 action 的精确策略键与兼容别名
    @property
    def permission_scope(self) -> PermissionScope:
        return self.action.permission_scope(
            self.spec.name,
            is_family=self.spec.is_action_family,
        )

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

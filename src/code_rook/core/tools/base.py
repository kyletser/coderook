from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel

from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ResourceClaim,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolSpec,
)


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "conflict"
    error_type: str | None = None
    # 可选多模态附件：Anthropic 风格 image block dict，随下一次模型请求发送
    images: list[dict[str, object]] | None = None
    # 受管子进程的 CPU、内存、进程数和 wall-time 证据；非进程工具为空
    process_usage: dict[str, object] | None = None


class ToolRetryPolicy(StrEnum):
    NEVER = "never"
    RATE_LIMIT = "rate_limit"
    IDEMPOTENT = "idempotent"


# 工具副作用的本性，用于隔离、权限和并行决策
class ToolSideEffect(StrEnum):
    NONE = "none"             # 纯读，无任何副作用
    LOCAL_WRITE = "local_write"  # 仅写本工作区内文件
    EXTERNAL_WRITE = "external_write"  # 执行外部命令、调用外部服务或派生子进程


class BaseTool(ABC):
    name: str
    description: str
    input_schema: dict[str, object]
    params_model: ClassVar[type[BaseModel] | None] = None
    retry_policy: ClassVar[ToolRetryPolicy] = ToolRetryPolicy.NEVER
    # 工具的本性副作用，保守默认为外部写入：未显式声明的工具按"高权"处理
    side_effect: ClassVar[ToolSideEffect] = ToolSideEffect.EXTERNAL_WRITE
    # 仅在读且彼此输入无冲突时由 loop 并发执行；默认 False 表示串行
    can_parallel: ClassVar[bool] = False
    version: ClassVar[str] = "1"
    model_visible: ClassVar[bool] = True
    deferred: ClassVar[bool] = False
    allowed_callers: ClassVar[frozenset[ToolCaller]] = frozenset(
        {ToolCaller.MODEL, ToolCaller.INTERNAL}
    )
    spec_override: ClassVar[ToolSpec | None] = None
    # 工具级超时覆盖：None 沿用调用方默认；0 表示不限时（交互式等待场景）
    timeout_s: ClassVar[float | None] = None

    def can_retry(self, error_type: str) -> bool:
        if self.retry_policy == ToolRetryPolicy.IDEMPOTENT:
            return error_type in {"runtime_error", "rate_limited"}
        if self.retry_policy == ToolRetryPolicy.RATE_LIMIT:
            return error_type == "rate_limited"
        return False

    # 表明工具是否纯读：用于自动派生 read-only 子 Agent 工具集
    @property
    def is_read_only(self) -> bool:
        return self.side_effect == ToolSideEffect.NONE

    # 将旧工具副作用声明转换为 V2 capability，供 action-family 迁移期间兼容使用
    def _legacy_capabilities(self) -> frozenset[ToolCapability]:
        if self.name == "bash":
            return frozenset({ToolCapability.PROCESS, ToolCapability.EXTERNAL})
        if self.side_effect == ToolSideEffect.NONE:
            capabilities = {ToolCapability.READ}
            if self.name.startswith("git_"):
                capabilities.add(ToolCapability.GIT)
            return frozenset(capabilities)
        if self.side_effect == ToolSideEffect.LOCAL_WRITE:
            return frozenset({ToolCapability.WRITE})
        return frozenset({ToolCapability.EXTERNAL})

    # 构建稳定 ToolSpec；显式 override 优先，否则适配旧平铺工具
    def build_spec(self) -> ToolSpec:
        if self.spec_override is not None:
            return self.spec_override
        capabilities = self._legacy_capabilities()
        return ToolSpec(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=self.input_schema,
            actions=(
                ToolActionSpec(
                    name="invoke",
                    description=self.description,
                    capabilities=capabilities,
                ),
            ),
            capabilities=capabilities,
            approval_requirement=(
                ApprovalRequirement.NEVER
                if self.is_read_only
                else ApprovalRequirement.POLICY
            ),
            parallel_policy=(
                ParallelPolicy.SAFE if self.can_parallel else ParallelPolicy.SERIAL
            ),
            allowed_callers=self.allowed_callers,
            model_visible=self.model_visible,
            deferred=self.deferred,
        )

    # 声明本次调用占用的资源；旧工具默认无显式 claim
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...]:
        return ()

    # 返回审批界面可消费的结构化上下文，普通工具默认没有额外上下文
    def approval_context(self, params: dict[str, object]) -> dict[str, object] | None:
        return None

    # 将公开 family 调用解析为恰好执行一次的真实 backend 与规范化参数
    def execution_target(
        self,
        params: dict[str, object],
    ) -> tuple[BaseTool, dict[str, object]]:
        return self, dict(params)

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...

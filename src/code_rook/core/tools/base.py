from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # "runtime_error" | "timeout" | "schema_error" | "permission_denied" | "conflict"
    error_type: str | None = None


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

    # 执行工具调用，返回结果或错误
    @abstractmethod
    async def invoke(self, params: dict[str, object]) -> ToolResult: ...
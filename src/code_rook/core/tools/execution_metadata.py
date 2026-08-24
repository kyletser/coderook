from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ToolExecutionMetadata:
    program_id: str = ""
    parent_tool_call_id: str = ""
    node_id: str = ""
    commit_order: int = 0


_CURRENT_METADATA: ContextVar[ToolExecutionMetadata] = ContextVar(
    "coderook_tool_execution_metadata",
    default=ToolExecutionMetadata(),
)
_CURRENT_INVOCATION_ID: ContextVar[str] = ContextVar(
    "coderook_tool_invocation_id",
    default="",
)


# 返回当前协程工具调用继承的 Tool Program 关联元数据
def current_tool_metadata() -> ToolExecutionMetadata:
    return _CURRENT_METADATA.get()


# 返回当前工具实现对应的外层 Tool Call ID
def current_tool_invocation_id() -> str:
    return _CURRENT_INVOCATION_ID.get()


# 在当前异步上下文内临时绑定 Tool Program 子调用元数据
@contextmanager
def tool_execution_metadata(metadata: ToolExecutionMetadata) -> Iterator[None]:
    token = _CURRENT_METADATA.set(metadata)
    try:
        yield
    finally:
        _CURRENT_METADATA.reset(token)


# 在执行工具实现期间绑定外层 Tool Call ID，供安全编排器建立父子关系
@contextmanager
def tool_invocation(tool_call_id: str) -> Iterator[None]:
    token = _CURRENT_INVOCATION_ID.set(tool_call_id)
    try:
        yield
    finally:
        _CURRENT_INVOCATION_ID.reset(token)

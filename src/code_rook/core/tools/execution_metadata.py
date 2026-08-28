from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
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
_CURRENT_PROGRESS_REPORTER: ContextVar[
    Callable[[str, int], Awaitable[None]] | None
] = ContextVar("coderook_tool_progress_reporter", default=None)


# 返回当前协程工具调用继承的 Tool Program 关联元数据
def current_tool_metadata() -> ToolExecutionMetadata:
    return _CURRENT_METADATA.get()


# 返回当前工具实现对应的外层 Tool Call ID
def current_tool_invocation_id() -> str:
    return _CURRENT_INVOCATION_ID.get()


# 把有界工具输出尾部交给当前调用的进度发布器
async def report_tool_progress(output_tail: str, total_bytes: int) -> None:
    reporter = _CURRENT_PROGRESS_REPORTER.get()
    if reporter is not None:
        await reporter(output_tail, total_bytes)


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
def tool_invocation(
    tool_call_id: str,
    *,
    progress: Callable[[str, int], Awaitable[None]] | None = None,
) -> Iterator[None]:
    token = _CURRENT_INVOCATION_ID.set(tool_call_id)
    progress_token = _CURRENT_PROGRESS_REPORTER.set(progress)
    try:
        yield
    finally:
        _CURRENT_PROGRESS_REPORTER.reset(progress_token)
        _CURRENT_INVOCATION_ID.reset(token)

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

type CompletionStatus = Literal[
    "completed",
    "tool_use",
    "length",
    "incomplete",
    "content_filtered",
    "failed",
    "cancelled",
    "transport_error",
]


# 将三种模型协议的终止原因映射到统一完成状态
def completion_status_from_reason(
    reason: str | None,
    *,
    has_tool_calls: bool = False,
    incomplete_reason: str | None = None,
) -> CompletionStatus:
    normalized = (reason or "").strip().lower()
    detail = (incomplete_reason or "").strip().lower()
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized == "transport_error":
        return "transport_error"
    if normalized in {"content_filter", "content_filtered", "refusal"} or detail in {
        "content_filter",
        "content_filtered",
    }:
        return "content_filtered"
    if normalized in {"max_tokens", "length", "max_output_tokens"} or detail in {
        "max_tokens",
        "max_output_tokens",
    }:
        return "length"
    if normalized in {"incomplete", "in_progress", "queued"}:
        return "incomplete"
    if has_tool_calls or normalized in {"tool_use", "tool_calls", "function_call"}:
        return "tool_use"
    if normalized in {"end_turn", "stop", "stop_sequence", "completed"}:
        return "completed"
    return "incomplete"


# 在 Provider 未返回 usage 时按完整请求文本确定性估算输入 token
def estimate_request_input_tokens(
    messages: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    system: str,
) -> int:
    payload = {
        "system": system,
        "messages": messages,
        "tools": tool_schemas,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return max(1, len(encoded) // 4)


@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    context_pct: float = 0.0


@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass
class LlmResponse:
    stop_reason: str  # 兼容字段；统一语义以 completion_status 为准
    tool_calls: list[ToolCallBlock] = field(default_factory=list)
    text: str = ""
    usage: UsageStats | None = None
    completion_status: CompletionStatus | None = None
    completion_reason: str = ""
    # thinking blocks from extended thinking — must be preserved verbatim in conversation history
    thinking_blocks: list[dict[str, object]] = field(default_factory=list)

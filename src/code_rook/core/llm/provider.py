from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx

from code_rook.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.budget import clamp_output_token_limit
from code_rook.core.llm.errors import ProviderRequestError
from code_rook.core.llm.types import (
    LlmResponse,
    ToolCallBlock,
    UsageStats,
    completion_status_from_reason,
)
from code_rook.core.llm.wire import merge_consecutive_user_messages

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-4-7": 200_000,
}

# thinking 档位 -> (budget_tokens, max_tokens)；max_tokens 必须大于 budget_tokens
_THINKING_BUDGETS: dict[str, tuple[int, int]] = {
    "low": (4_096, 12_288),
    "medium": (8_192, 16_384),
    "high": (16_384, 24_576),
}

log = logging.getLogger(__name__)


# 返回指定模型的最大 context window token 数
def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, 200_000)


# 把 thinking 档位映射为 Anthropic 的 budget_tokens 与配套 max_tokens
def anthropic_thinking_params(
    thinking: str,
) -> tuple[int, dict[str, object] | None]:
    mapped = _THINKING_BUDGETS.get(thinking)
    if mapped is None:
        return 8_192, None
    budget, max_tokens = mapped
    return max_tokens, {"type": "enabled", "budget_tokens": budget}


# 在最后一个含 tool_result 的 user 消息末块追加增量 cache breakpoint
def with_incremental_cache_breakpoint(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    result = list(messages)
    for position in range(len(result) - 1, -1, -1):
        message = result[position]
        if message.get("role") != "user":
            continue
        raw_content = message.get("content")
        if not isinstance(raw_content, list) or not raw_content:
            continue
        blocks: list[object] = list(raw_content)
        last = blocks[-1]
        if (
            isinstance(last, dict)
            and last.get("type") == "tool_result"
            and "cache_control" not in last
        ):
            new_last = dict(last)
            new_last["cache_control"] = {"type": "ephemeral"}
            blocks[-1] = new_last
            result[position] = {**message, "content": blocks}
        return result
    return result


_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(
        self,
        model: str,
        client: Any = None,
        *,
        api_key: str | None = None,
        base_url: str = "",
        context_window: int | None = None,
        thinking: str = "off",
        supports_prompt_cache: bool = True,
        temperature: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._client: Any
        if client is None:
            resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not resolved_key:
                raise SystemExit("ANTHROPIC_API_KEY not set")
            if base_url:
                self._client = anthropic.AsyncAnthropic(
                    api_key=resolved_key,
                    base_url=base_url,
                    max_retries=0,
                    default_headers=dict(headers or {}),
                )
            else:
                self._client = anthropic.AsyncAnthropic(
                    api_key=resolved_key,
                    max_retries=0,
                    default_headers=dict(headers or {}),
                )
        else:
            self._client = (
                client.with_options(
                    max_retries=0,
                    default_headers=dict(headers or {}),
                )
                if isinstance(client, anthropic.AsyncAnthropic) else client
            )
        self._model = model
        self._context_window = context_window
        self._thinking = thinking
        self._supports_prompt_cache = supports_prompt_cache
        self._temperature = temperature

    @property
    # 返回当前 Provider 模型的窗口，供首次请求前进行压缩判断。
    def context_window(self) -> int:
        return self._context_window or _context_window(self._model)

    # 单次流式调用 Anthropic，错误交给主循环统一重试，不在适配器内隐藏重试
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> LlmResponse:
        resolved_model = model or self._model
        # 内部注入（图片/steering/续跑）会产生连续 user 消息，Anthropic Messages API 硬拒角色交替
        messages = merge_consecutive_user_messages(messages)
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=resolved_model, strategy="static", ts=_now())
        )

        effective_thinking = thinking if thinking is not None else self._thinking
        max_tokens, thinking_param = anthropic_thinking_params(effective_thinking)
        max_tokens = clamp_output_token_limit(max_tokens)
        thinking_budget = (
            thinking_param.get("budget_tokens") if thinking_param is not None else None
        )
        if (
            isinstance(thinking_budget, int)
            and thinking_budget >= max_tokens
        ):
            thinking_param = None
        request_messages = (
            with_incremental_cache_breakpoint(messages)
            if self._supports_prompt_cache
            else messages
        )

        system_blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": system or _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": resolved_model,
            "max_tokens": max_tokens,
            "system": system_blocks,
            "messages": request_messages,
        }
        if thinking_param is not None:
            kwargs["thinking"] = thinking_param
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []
        final_message: Any = None
        stream: Any = None

        failure: ProviderRequestError | None = None
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                async for text in stream.text_stream:
                    await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                    text_parts.append(text)
                final_message = await stream.get_final_message()
        except (httpx.HTTPError, anthropic.APIError) as exc:
            failure = ProviderRequestError("Anthropic", exc)
        finally:
            # 超时和取消也保存 SDK 已接收的推理及工具参数，不只处理 HTTP 异常
            if final_message is None and stream is not None:
                try:
                    snapshot = stream.current_message_snapshot
                except (AttributeError, AssertionError):
                    snapshot = None
                if isinstance(snapshot, anthropic.types.Message):
                    bus.record_stream_fragment({
                        "wire_format": "anthropic", "partial_message": snapshot.model_dump(),
                    })
        if failure is not None:
            raise failure
        if final_message is None:
            raise RuntimeError("Anthropic returned no final message")

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        window = self._context_window or _context_window(resolved_model)
        # Anthropic 的 input_tokens 不含缓存命中/写入部分；真实上下文占用必须三者相加，
        # 否则开启 prompt cache 后 context_pct 崩塌、自动压缩永不触发直至溢出
        context_tokens = usage.input_tokens + cache_read + cache_create
        context_pct = context_tokens / window

        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
                model=resolved_model,
                ts=_now(),
            )
        )

        completion_status = completion_status_from_reason(
            final_message.stop_reason,
            has_tool_calls=any(
                block.type == "tool_use" for block in final_message.content
            ),
        )
        tool_calls: list[ToolCallBlock] = []
        thinking_blocks: list[dict[str, object]] = []
        for block in final_message.content:
            if block.type == "tool_use" and completion_status == "tool_use":
                tool_calls.append(
                    ToolCallBlock(id=block.id, name=block.name, input=dict(block.input))
                )
            elif block.type == "thinking":
                # thinking blocks must be passed back verbatim in subsequent requests
                thinking_blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                })
            elif block.type == "redacted_thinking":
                thinking_blocks.append({"type": "redacted_thinking", "data": block.data})

        return LlmResponse(
            stop_reason=(
                "tool_use"
                if completion_status == "tool_use"
                else "end_turn"
                if completion_status == "completed"
                else "max_tokens"
                if completion_status == "length"
                else completion_status
            ),
            tool_calls=tool_calls,
            text="".join(text_parts),
            completion_status=completion_status,
            completion_reason=final_message.stop_reason or "",
            thinking_blocks=thinking_blocks,
            usage=UsageStats(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
            ),
        )

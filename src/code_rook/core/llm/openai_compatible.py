from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from code_rook.core.bus.events import (
    LlmModelSelectedEvent,
    LlmReasoningEvent,
    LlmTokenEvent,
    LlmUsageEvent,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.budget import clamp_output_token_limit
from code_rook.core.llm.types import (
    LlmResponse,
    ToolCallBlock,
    UsageStats,
    completion_status_from_reason,
    estimate_request_input_tokens,
)
from code_rook.core.llm.wire import merge_consecutive_user_messages

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-pro": 1_048_576,
    "deepseek-v4-flash": 1_048_576,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-luna": 1_050_000,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, 128_000)


@dataclass
class _StreamResult:
    text: str
    reasoning: str
    raw_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    streamed: bool = False
    finish_received: bool = True
    done_received: bool = True

    @property
    # 仅同时收到语义终态与传输终止标记时，流式响应才算完整关闭
    def terminal_received(self) -> bool:
        return self.finish_received and self.done_received


class OpenAICompatibleProvider:
    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key_env: str,
        api_key: str | None = None,
        api_key_required: bool = True,
        use_max_completion_tokens: bool = False,
        context_window: int | None = None,
        thinking: str = "off",
        temperature: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise SystemExit("CODEROOK_LLM_BASE_URL not set")
        resolved_key = api_key or os.environ.get(api_key_env)
        if api_key_required and not resolved_key:
            raise SystemExit(f"{api_key_env} not set")
        self._model = model
        self._base_url = base_url
        self._api_key = resolved_key or ""
        self._use_max_completion_tokens = use_max_completion_tokens
        self._context_window = context_window
        self._thinking = thinking
        self._temperature = temperature
        self._client = client

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
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id,
                model=resolved_model,
                strategy="openai_compatible",
                ts=_now(),
            )
        )

        is_deepseek = "api.deepseek.com" in self._base_url and resolved_model.startswith(
            "deepseek-"
        )
        if thinking is not None:
            effective_thinking = thinking
        elif self._thinking != "off":
            effective_thinking = self._thinking
        elif is_deepseek:
            # 兼容旧行为：DeepSeek 域名未显式配置时保持默认高推理
            effective_thinking = "high"
        else:
            effective_thinking = "off"
        payload: dict[str, object] = {
            "model": resolved_model,
            "messages": _to_openai_messages(
                merge_consecutive_user_messages(messages),
                system or _SYSTEM_PROMPT,
                include_reasoning=effective_thinking != "off",
            ),
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if effective_thinking in {"low", "medium", "high"}:
            payload["reasoning_effort"] = effective_thinking
            if is_deepseek:
                payload["thinking"] = {"type": "enabled"}
        max_tokens_field = (
            "max_completion_tokens" if self._use_max_completion_tokens else "max_tokens"
        )
        payload[max_tokens_field] = clamp_output_token_limit(8192)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        tools = _to_openai_tools(tool_schemas)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        if self._client is None:
            async with httpx.AsyncClient(timeout=120.0) as client:
                result = await self._post(client, payload, headers, bus, run_id, step)
        else:
            result = await self._post(self._client, payload, headers, bus, run_id, step)

        text = result.text
        reasoning = result.reasoning
        completion_status = completion_status_from_reason(
            result.finish_reason,
            has_tool_calls=bool(result.raw_tool_calls),
        )
        if result.streamed and not result.terminal_received:
            completion_status = "transport_error"
        tool_calls = (
            _parse_tool_calls(result.raw_tool_calls)
            if completion_status in {"completed", "tool_use"}
            else []
        )
        if completion_status == "completed" and tool_calls:
            completion_status = "tool_use"
        if reasoning:
            await bus.publish(
                LlmReasoningEvent(
                    run_id=run_id,
                    content=reasoning,
                    ts=_now(),
                )
            )
        if text and not tool_calls and not result.streamed:
            await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))

        usage_raw = result.usage
        reported_input_tokens = int(
            usage_raw.get("prompt_tokens") or usage_raw.get("input_tokens") or 0
        )
        input_tokens = reported_input_tokens or estimate_request_input_tokens(
            messages,
            tool_schemas,
            system or "",
        )
        output_tokens = int(
            usage_raw.get("completion_tokens") or usage_raw.get("output_tokens") or 0
        )
        window = self._context_window or _context_window(resolved_model)
        context_pct = input_tokens / window
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=context_pct,
                model=resolved_model,
                ts=_now(),
            )
        )

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
            text=text,
            completion_status=completion_status,
            completion_reason=result.finish_reason or "",
            thinking_blocks=([{"type": "thinking", "thinking": reasoning}] if reasoning else []),
            usage=UsageStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                context_pct=context_pct,
            ),
        )

    async def _post(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, object],
        headers: dict[str, str],
        bus: EventBus,
        run_id: str,
        step: int,
    ) -> _StreamResult:
        data: dict[str, Any] | None = None
        failure: str | None = None
        try:
            async with client.stream(
                "POST", self._base_url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type.lower():
                    return await self._consume_sse(response, bus, run_id)
                raw = await response.aread()
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = dict(parsed)
                else:
                    failure = "OpenAI-compatible provider returned an invalid response"
        except httpx.HTTPStatusError as exc:
            log.error(
                "openai-compatible request failed run_id=%s step=%d status=%s",
                run_id,
                step,
                exc.response.status_code,
            )
            failure = f"OpenAI-compatible request failed (HTTP {exc.response.status_code})"
        except httpx.HTTPError:
            log.error(
                "openai-compatible transport failed run_id=%s step=%d",
                run_id,
                step,
            )
            failure = "OpenAI-compatible request failed"
        except ValueError:
            failure = "OpenAI-compatible provider returned invalid JSON"
        if failure is not None:
            raise RuntimeError(failure)
        assert data is not None
        return self._from_full_response(data)

    # 解析 SSE 增量块：正文实时投影为 token 事件，reasoning 与工具调用片段累积合并
    async def _consume_sse(
        self,
        response: httpx.Response,
        bus: EventBus,
        run_id: str,
    ) -> _StreamResult:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        finish_received = False
        done_received = False
        async for line in response.aiter_lines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data_raw = stripped[5:].strip()
            if data_raw == "[DONE]":
                done_received = True
                break
            try:
                chunk = json.loads(data_raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            chunk_usage = chunk.get("usage")
            if isinstance(chunk_usage, dict) and chunk_usage:
                usage = chunk_usage
            choices = chunk.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            first = choices[0]
            if not isinstance(first, dict):
                continue
            raw_finish = first.get("finish_reason")
            if isinstance(raw_finish, str) and raw_finish.strip():
                finish_reason = raw_finish.strip()
                finish_received = True
            delta = first.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
                await bus.publish(LlmTokenEvent(run_id=run_id, token=content, ts=_now()))
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
                bus.mark_stream_activity(len(reasoning.encode("utf-8")))
            raw_calls = delta.get("tool_calls")
            if isinstance(raw_calls, list):
                for raw_call in raw_calls:
                    if not isinstance(raw_call, dict):
                        continue
                    raw_index = raw_call.get("index")
                    index = int(raw_index) if isinstance(raw_index, int) else 0
                    slot = tool_acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    call_id = raw_call.get("id")
                    if isinstance(call_id, str) and call_id:
                        slot["id"] = call_id
                        bus.mark_stream_activity(len(call_id.encode("utf-8")))
                    function = raw_call.get("function")
                    if isinstance(function, dict):
                        name = function.get("name")
                        if isinstance(name, str) and name:
                            slot["name"] = name
                            bus.mark_stream_activity(len(name.encode("utf-8")))
                        arguments = function.get("arguments")
                        if isinstance(arguments, str) and arguments:
                            slot["arguments"] += arguments
                            bus.mark_stream_activity(len(arguments.encode("utf-8")))
        raw_tool_calls: list[dict[str, Any]] = [
            {
                "id": slot["id"],
                "function": {"name": slot["name"], "arguments": slot["arguments"]},
            }
            for _, slot in sorted(tool_acc.items())
        ]
        return _StreamResult(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            raw_tool_calls=raw_tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            streamed=True,
            finish_received=finish_received,
            done_received=done_received,
        )

    # 从非流式完整响应提取正文、reasoning、工具调用与 usage
    def _from_full_response(self, data: dict[str, Any]) -> _StreamResult:
        choices = data.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            message = {}
        usage = data.get("usage")
        raw_finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        raw_calls = message.get("tool_calls")
        return _StreamResult(
            text=str(message.get("content") or ""),
            reasoning=str(message.get("reasoning_content") or ""),
            raw_tool_calls=list(raw_calls) if isinstance(raw_calls, list) else [],
            usage=usage if isinstance(usage, dict) else {},
            finish_reason=raw_finish if isinstance(raw_finish, str) else None,
            streamed=False,
            finish_received=True,
            done_received=True,
        )


def _to_openai_tools(tool_schemas: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": _compatible_tool_schema(tool.get("input_schema", {"type": "object"})),
            },
        }
        for tool in tool_schemas
    ]


# 将 action-family 的 oneOf 降级为兼容 OpenAI-compatible 后端的单对象 schema
def _compatible_tool_schema(raw_schema: object) -> object:
    if not isinstance(raw_schema, dict):
        return raw_schema
    schema = deepcopy(raw_schema)
    variants = schema.get("oneOf")
    if not isinstance(variants, list) or not variants:
        return schema

    action_names: list[str] = []
    merged_properties: dict[str, object] = {}
    required_sets: list[set[str]] = []
    requirements: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict):
            return schema
        properties = variant.get("properties")
        if not isinstance(properties, dict):
            return schema
        action_schema = properties.get("action")
        if not isinstance(action_schema, dict):
            return schema
        action_values = action_schema.get("enum")
        if not isinstance(action_values, list) or len(action_values) != 1:
            return schema
        action_name = action_values[0]
        if not isinstance(action_name, str) or not action_name:
            return schema
        action_names.append(action_name)

        raw_required = variant.get("required", [])
        if not isinstance(raw_required, list) or not all(
            isinstance(item, str) for item in raw_required
        ):
            return schema
        required = set(raw_required)
        required_sets.append(required)
        action_required = sorted(required - {"action"})
        requirements.append(
            f"{action_name}: {', '.join(action_required) if action_required else 'none'}"
        )
        for name, property_schema in properties.items():
            if name == "action":
                continue
            merged_properties.setdefault(str(name), deepcopy(property_schema))

    common_required = set.intersection(*required_sets) if required_sets else {"action"}
    merged_properties["action"] = {
        "type": "string",
        "enum": action_names,
        "description": "Required fields by action: " + "; ".join(requirements) + ".",
    }
    schema.pop("oneOf", None)
    schema["type"] = "object"
    schema["properties"] = {
        "action": merged_properties.pop("action"),
        **merged_properties,
    }
    schema["required"] = [name for name in schema["properties"] if name in common_required]
    return schema


# 把内部 image block 转为 OpenAI data URI，结构不完整时返回 None
def _image_block_to_data_uri(block: dict[str, object]) -> str | None:
    source = block.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    media_type = str(source.get("media_type", ""))
    data = str(source.get("data", ""))
    if not media_type or not data:
        return None
    return f"data:{media_type};base64,{data}"


def _to_openai_messages(
    messages: list[dict[str, object]],
    system: str,
    *,
    include_reasoning: bool = False,
) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = [{"role": "system", "content": system}]
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = msg.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        if role == "assistant" and isinstance(content, list):
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: list[dict[str, object]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
                elif block_type == "thinking" and include_reasoning:
                    reasoning_parts.append(str(block.get("thinking", "")))
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(block.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name", "")),
                                "arguments": json.dumps(
                                    block.get("input", {}),
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    )
            row: dict[str, object] = {
                "role": "assistant",
                "content": "".join(text_parts) or None,
            }
            if tool_calls:
                row["tool_calls"] = tool_calls
            if reasoning_parts:
                row["reasoning_content"] = "".join(reasoning_parts)
            converted.append(row)
            continue

        if role == "user" and isinstance(content, list):
            normal_parts: list[str] = []
            image_parts: list[dict[str, object]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    converted.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id", "")),
                            "content": str(block.get("content", "")),
                        }
                    )
                elif block.get("type") == "text":
                    normal_parts.append(str(block.get("text", "")))
                elif block.get("type") == "image":
                    data_uri = _image_block_to_data_uri(block)
                    if data_uri is not None:
                        image_parts.append({"type": "image_url", "image_url": {"url": data_uri}})
            if image_parts:
                user_content: list[dict[str, object]] = []
                if normal_parts:
                    user_content.append({"type": "text", "text": "\n".join(normal_parts)})
                user_content.extend(image_parts)
                converted.append({"role": "user", "content": user_content})
            elif normal_parts:
                converted.append({"role": "user", "content": "\n".join(normal_parts)})
            continue

        converted.append({"role": role, "content": str(content)})
    return converted


# 严格解析完整工具参数，拒绝把截断或非对象 JSON 交给执行器
def _parse_tool_calls(raw_tool_calls: list[Any]) -> list[ToolCallBlock]:
    tool_calls: list[ToolCallBlock] = []
    for raw in raw_tool_calls:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        if not isinstance(function, dict):
            continue
        arguments_raw = function.get("arguments") or "{}"
        if isinstance(arguments_raw, str):
            try:
                arguments = json.loads(arguments_raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "OpenAI-compatible provider returned invalid tool arguments"
                ) from exc
        elif isinstance(arguments_raw, dict):
            arguments = arguments_raw
        else:
            raise RuntimeError("OpenAI-compatible provider returned invalid tool arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError("OpenAI-compatible tool arguments must be an object")
        tool_calls.append(
            ToolCallBlock(
                id=str(raw.get("id", "")),
                name=str(function.get("name", "")),
                input=dict(arguments),
            )
        )
    return tool_calls

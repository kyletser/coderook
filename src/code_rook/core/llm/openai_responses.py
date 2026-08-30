from __future__ import annotations

import json
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
)
from code_rook.core.llm.wire import merge_consecutive_user_messages

_DEFAULT_CONTEXT_WINDOW = 1_050_000


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将会话消息转换为 Responses API input item，保留函数调用配对
def _to_responses_input(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, str):
            items.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue
        text_parts: list[str] = []
        image_parts: list[dict[str, object]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(str(block["text"]))
            elif block_type == "image":
                source = block.get("source")
                if isinstance(source, dict) and source.get("type") == "base64":
                    media_type = str(source.get("media_type", ""))
                    data = str(source.get("data", ""))
                    if media_type and data:
                        image_parts.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{media_type};base64,{data}",
                            }
                        )
            elif block_type == "tool_use":
                items.append(
                    {
                        "type": "function_call",
                        "call_id": str(block.get("id", "")),
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(
                            block.get("input", {}),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    }
                )
            elif block_type == "tool_result":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(block.get("tool_use_id", "")),
                        "output": str(block.get("content", "")),
                    }
                )
        if text_parts or image_parts:
            if image_parts:
                parts: list[dict[str, object]] = [
                    {"type": "input_text", "text": "".join(text_parts)}
                ]
                parts.extend(image_parts)
                items.append({"role": role, "content": parts})
            else:
                items.append({"role": role, "content": "".join(text_parts)})
    return items


# 将 CodeRook 工具 schema 转换为 Responses function tool
def _to_responses_tools(tool_schemas: list[dict[str, object]]) -> list[dict[str, object]]:
    tools: list[dict[str, object]] = []
    for schema in tool_schemas:
        tools.append(
            {
                "type": "function",
                "name": str(schema.get("name", "")),
                "description": str(schema.get("description", "")),
                "parameters": schema.get("input_schema", {}),
                "strict": False,
            }
        )
    return tools


# 从 Responses output 提取正文、函数调用与可展示 reasoning summary
def _parse_output(
    output: object,
) -> tuple[str, list[ToolCallBlock], str]:
    if not isinstance(output, list):
        raise RuntimeError("OpenAI Responses returned an invalid output array")
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    calls: list[ToolCallBlock] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            raw_arguments = item.get("arguments", "{}")
            try:
                arguments = json.loads(str(raw_arguments))
            except json.JSONDecodeError as exc:
                raise RuntimeError("OpenAI Responses returned invalid tool arguments") from exc
            if not isinstance(arguments, dict):
                raise RuntimeError("OpenAI Responses tool arguments must be an object")
            calls.append(
                ToolCallBlock(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name", "")),
                    input=arguments,
                )
            )
        elif item_type == "message":
            content = item.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        text_parts.append(str(part["text"]))
        elif item_type == "reasoning":
            summary = item.get("summary", [])
            if isinstance(summary, list):
                reasoning_parts.extend(
                    str(part["text"])
                    for part in summary
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
    return "".join(text_parts), calls, "\n".join(reasoning_parts)


# 判断 Responses output 是否含函数调用，避免不完整参数进入 JSON 解析和执行链
def _output_has_tool_calls(output: object) -> bool:
    return isinstance(output, list) and any(
        isinstance(item, dict) and item.get("type") == "function_call" for item in output
    )


# 移除非完成响应中的潜在半截函数调用，仅保留可安全展示的文本和推理
def _visible_output_only(output: object) -> object:
    if not isinstance(output, list):
        return output
    return [
        item for item in output if not isinstance(item, dict) or item.get("type") != "function_call"
    ]


class OpenAIResponsesProvider:
    # 初始化 Responses API 端点、模型、凭据和可注入 HTTP client
    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        api_key_required: bool = True,
        context_window: int | None = None,
        thinking: str = "off",
        temperature: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        if api_key_required and not api_key:
            raise SystemExit("API key not set")
        self._api_key = api_key
        self._context_window = context_window or _DEFAULT_CONTEXT_WINDOW
        self._thinking = thinking
        self._temperature = temperature
        self._client = client

    # 调用 Responses API 并把正文、reasoning summary、工具调用和 usage 投影为统一事件
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
        # 与 Anthropic 同理：连续 user 消息在请求组装处归一，内部注入不再打断角色交替
        messages = merge_consecutive_user_messages(messages)
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id,
                model=resolved_model,
                strategy="openai_responses",
                ts=_now(),
            )
        )
        effective_thinking = thinking if thinking is not None else self._thinking
        payload: dict[str, object] = {
            "model": resolved_model,
            "input": _to_responses_input(messages),
            "instructions": system or "",
            "max_output_tokens": clamp_output_token_limit(8192),
            "store": False,
            "stream": True,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if effective_thinking in {"low", "medium", "high"}:
            payload["reasoning"] = {"effort": effective_thinking}
        tools = _to_responses_tools(tool_schemas)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._client is None:
            async with httpx.AsyncClient(timeout=120.0) as client:
                data, streamed = await self._post(client, payload, headers, bus, run_id)
        else:
            data, streamed = await self._post(self._client, payload, headers, bus, run_id)
        output = data.get("output")
        status_value = data.get("status")
        if isinstance(status_value, str) and status_value.strip():
            status_present = True
            raw_status = status_value.strip()
        else:
            status_present = False
            raw_status = "transport_error"
        incomplete_details = data.get("incomplete_details")
        incomplete_reason = (
            str(incomplete_details.get("reason") or "")
            if isinstance(incomplete_details, dict)
            else ""
        )
        completion_status = completion_status_from_reason(
            raw_status,
            has_tool_calls=_output_has_tool_calls(output),
            incomplete_reason=incomplete_reason,
        )
        parsed_output = (
            output
            if completion_status in {"completed", "tool_use"}
            else _visible_output_only(output)
        )
        text, tool_calls, reasoning = _parse_output(parsed_output)
        if completion_status == "completed" and tool_calls:
            completion_status = "tool_use"
        if reasoning:
            await bus.publish(LlmReasoningEvent(run_id=run_id, content=reasoning, ts=_now()))
        if text and not tool_calls and not streamed:
            await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
        usage = data.get("usage", {})
        usage_value = usage if isinstance(usage, dict) else {}
        input_tokens = int(usage_value.get("input_tokens", 0) or 0)
        output_tokens = int(usage_value.get("output_tokens", 0) or 0)
        input_details = usage_value.get("input_tokens_details", {})
        details = input_details if isinstance(input_details, dict) else {}
        cache_read = int(details.get("cached_tokens", 0) or 0)
        context_pct = input_tokens / self._context_window
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
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
            completion_reason=(
                incomplete_reason or (raw_status if status_present else "missing_response_status")
            ),
            thinking_blocks=([{"type": "thinking", "thinking": reasoning}] if reasoning else []),
            usage=UsageStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                context_pct=context_pct,
            ),
        )

    # 发送流式请求；SSE 增量解析，非 SSE 响应降级为整体 JSON（兼容注入的测试 client）
    async def _post(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, object],
        headers: dict[str, str],
        bus: EventBus,
        run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        data: Any = None
        try:
            async with client.stream(
                "POST", self._base_url, json=payload, headers=headers
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type.lower():
                    return await self._consume_sse(response, bus, run_id)
                raw = await response.aread()
                data = json.loads(raw)
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"OpenAI Responses request failed (HTTP {exc.response.status_code})"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("OpenAI Responses request failed") from exc
        if not isinstance(data, dict):
            raise RuntimeError("OpenAI Responses returned an invalid response object")
        return data, False

    # 解析 Responses SSE 事件流；completed 事件给出完整响应，缺失时用增量累积兜底
    async def _consume_sse(
        self,
        response: httpx.Response,
        bus: EventBus,
        run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        completed: dict[str, Any] | None = None
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[str, dict[str, str]] = {}
        async for line in response.aiter_lines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            data_raw = stripped[5:].strip()
            if data_raw == "[DONE]":
                break
            try:
                event = json.loads(data_raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    text_parts.append(delta)
                    await bus.publish(LlmTokenEvent(run_id=run_id, token=delta, ts=_now()))
            elif event_type == "response.reasoning_summary_text.delta":
                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    reasoning_parts.append(delta)
            elif event_type == "response.output_item.added":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    slot = calls.setdefault(
                        str(item.get("id") or ""),
                        {"call_id": "", "name": "", "arguments": ""},
                    )
                    slot["call_id"] = str(item.get("call_id") or "")
                    slot["name"] = str(item.get("name") or "")
            elif event_type == "response.function_call_arguments.delta":
                key = str(event.get("item_id") or event.get("output_index") or "")
                slot = calls.setdefault(key, {"call_id": "", "name": "", "arguments": ""})
                delta = event.get("delta")
                if isinstance(delta, str):
                    slot["arguments"] += delta
            elif event_type in {
                "response.completed",
                "response.incomplete",
                "response.failed",
                "response.cancelled",
            }:
                full = event.get("response")
                if isinstance(full, dict):
                    completed = full
        if completed is not None:
            return completed, True
        output: list[dict[str, Any]] = []
        for slot in calls.values():
            output.append(
                {
                    "type": "function_call",
                    "call_id": slot["call_id"],
                    "name": slot["name"],
                    "arguments": slot["arguments"],
                }
            )
        if text_parts:
            output.append(
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "".join(text_parts)}],
                }
            )
        if reasoning_parts:
            output.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "".join(reasoning_parts)}],
                }
            )
        return {
            "status": "transport_error",
            "output": output,
            "usage": {},
        }, True

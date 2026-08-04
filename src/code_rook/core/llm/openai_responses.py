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
from code_rook.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

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
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(str(block["text"]))
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
        if text_parts:
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


class OpenAIResponsesProvider:
    # 初始化 Responses API 端点、模型、凭据和可注入 HTTP client
    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key: str,
        context_window: int | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._context_window = context_window or _DEFAULT_CONTEXT_WINDOW
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
    ) -> LlmResponse:
        resolved_model = model or self._model
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id,
                model=resolved_model,
                strategy="openai_responses",
                ts=_now(),
            )
        )
        payload: dict[str, object] = {
            "model": resolved_model,
            "input": _to_responses_input(messages),
            "instructions": system or "",
            "max_output_tokens": 8192,
            "store": False,
        }
        tools = _to_responses_tools(tool_schemas)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._client is None:
            async with httpx.AsyncClient(timeout=120.0) as client:
                data = await self._post(client, payload, headers)
        else:
            data = await self._post(self._client, payload, headers)
        text, tool_calls, reasoning = _parse_output(data.get("output"))
        if reasoning:
            await bus.publish(
                LlmReasoningEvent(run_id=run_id, content=reasoning, ts=_now())
            )
        if text and not tool_calls:
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
                ts=_now(),
            )
        )
        return LlmResponse(
            stop_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls,
            text=text,
            usage=UsageStats(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
                context_pct=context_pct,
            ),
        )

    # 发送单次有界 HTTP 请求，并将外部错误转换为不含响应正文的安全异常
    async def _post(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, object],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"OpenAI Responses request failed (HTTP {exc.response.status_code})"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("OpenAI Responses request failed") from exc
        if not isinstance(data, dict):
            raise RuntimeError("OpenAI Responses returned an invalid response object")
        return data

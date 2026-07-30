from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from code_rook.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a helpful AI assistant. "
    "Use the available tools to complete the user's goal. "
    "When the goal is fully achieved, respond with a final answer and do not call any more tools."
)

_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-pro": 128_000,
    "gpt-5.6-sol": 1_050_000,
    "gpt-5.6-terra": 1_050_000,
    "gpt-5.6-luna": 1_050_000,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _context_window(model: str) -> int:
    return _MODEL_CONTEXT_WINDOWS.get(model, 128_000)


class OpenAICompatibleProvider:
    def __init__(
        self,
        model: str,
        *,
        base_url: str,
        api_key_env: str,
        api_key: str | None = None,
        use_max_completion_tokens: bool = False,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise SystemExit("CODEROOK_LLM_BASE_URL not set")
        resolved_key = api_key or os.environ.get(api_key_env)
        if not resolved_key:
            raise SystemExit(f"{api_key_env} not set")
        self._model = model
        self._base_url = base_url
        self._api_key = resolved_key
        self._use_max_completion_tokens = use_max_completion_tokens
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

        payload: dict[str, object] = {
            "model": resolved_model,
            "messages": _to_openai_messages(messages, system or _SYSTEM_PROMPT),
        }
        max_tokens_field = (
            "max_completion_tokens" if self._use_max_completion_tokens else "max_tokens"
        )
        payload[max_tokens_field] = 8192
        tools = _to_openai_tools(tool_schemas)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        if self._client is None:
            async with httpx.AsyncClient(timeout=120.0) as client:
                data = await self._post(client, payload, headers, run_id, step)
        else:
            data = await self._post(self._client, payload, headers, run_id, step)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = str(message.get("content") or "")
        if text:
            await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))

        usage_raw = data.get("usage") or {}
        input_tokens = int(usage_raw.get("prompt_tokens") or usage_raw.get("input_tokens") or 0)
        output_tokens = int(
            usage_raw.get("completion_tokens") or usage_raw.get("output_tokens") or 0
        )
        context_pct = input_tokens / _context_window(resolved_model)
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=context_pct,
                ts=_now(),
            )
        )

        tool_calls = _parse_tool_calls(message.get("tool_calls") or [])
        return LlmResponse(
            stop_reason="tool_use" if tool_calls else "end_turn",
            tool_calls=tool_calls,
            text=text,
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
        run_id: str,
        step: int,
    ) -> dict[str, Any]:
        try:
            response = await client.post(self._base_url, json=payload, headers=headers)
            response.raise_for_status()
            return dict(response.json())
        except httpx.HTTPStatusError as exc:
            log.error(
                "openai-compatible request failed run_id=%s step=%d status=%s body=%s",
                run_id,
                step,
                exc.response.status_code,
                exc.response.text[:1000],
            )
            raise


def _to_openai_tools(tool_schemas: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object"}),
            },
        }
        for tool in tool_schemas
    ]


def _to_openai_messages(
    messages: list[dict[str, object]],
    system: str,
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
            tool_calls: list[dict[str, object]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))
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
            converted.append(row)
            continue

        if role == "user" and isinstance(content, list):
            normal_parts: list[str] = []
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
            if normal_parts:
                converted.append({"role": "user", "content": "\n".join(normal_parts)})
            continue

        converted.append({"role": role, "content": str(content)})
    return converted


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
            except json.JSONDecodeError:
                arguments = {"_raw_arguments": arguments_raw}
        elif isinstance(arguments_raw, dict):
            arguments = arguments_raw
        else:
            arguments = {}
        tool_calls.append(
            ToolCallBlock(
                id=str(raw.get("id", "")),
                name=str(function.get("name", "")),
                input=dict(arguments),
            )
        )
    return tool_calls

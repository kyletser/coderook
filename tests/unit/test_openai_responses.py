from __future__ import annotations

import json

import httpx

from code_rook.core.events.bus import EventBus
from code_rook.core.llm.openai_responses import OpenAIResponsesProvider


# 功能：验证 Responses Provider 发送显式 wire payload 并解析函数调用
# 设计：MockTransport 检查 instructions、store、function schema，返回 function_call 与 usage
async def test_responses_provider_roundtrips_function_call() -> None:
    requests: list[httpx.Request] = []

    # 捕获请求并返回函数调用响应
    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call-1",
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 40},
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIResponsesProvider(
            "gpt-test",
            base_url="https://api.example/v1/responses",
            api_key="secret-key",
            context_window=1_000,
            client=client,
        )
        result = await provider.chat(
            [{"role": "user", "content": "read it"}],
            [
                {
                    "name": "read_file",
                    "description": "Read a file",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                }
            ],
            EventBus(),
            "run-1",
            system="system prompt",
        )

    payload = json.loads(requests[0].content)
    assert requests[0].headers["Authorization"] == "Bearer secret-key"
    assert payload["instructions"] == "system prompt"
    assert payload["store"] is False
    assert payload["tools"][0]["name"] == "read_file"
    assert result.stop_reason == "tool_use"
    assert result.tool_calls[0].input == {"path": "README.md"}
    assert result.usage is not None and result.usage.cache_read_input_tokens == 40


# 功能：验证历史 tool_use/tool_result 转换为 Responses 函数调用配对
# 设计：第二次请求带已有调用和结果，检查 input 中 call_id 一致且最终正文正确解析
async def test_responses_provider_preserves_tool_result_pairing() -> None:
    captured: list[dict[str, object]] = []

    # 捕获 JSON 后返回文本消息
    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "read_file",
                    "input": {"path": "README.md"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "contents",
                }
            ],
        },
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIResponsesProvider(
            "gpt-test",
            base_url="https://api.example/v1/responses",
            api_key="secret-key",
            client=client,
        )
        result = await provider.chat(messages, [], EventBus(), "run-2")

    function_call = captured[0]["input"][0]  # type: ignore[index]
    function_output = captured[0]["input"][1]  # type: ignore[index]
    assert function_call["call_id"] == function_output["call_id"] == "call-1"
    assert result.text == "done"
    assert result.stop_reason == "end_turn"


# 功能：验证 HTTP 错误异常不会包含服务端正文或 API key
# 设计：响应体故意回显 secret，断言抛出的安全异常只含状态码
async def test_responses_provider_redacts_http_error_body() -> None:
    # 返回回显密钥的认证失败响应
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            request=request,
            text="invalid secret-key",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAIResponsesProvider(
            "gpt-test",
            base_url="https://api.example/v1/responses",
            api_key="secret-key",
            client=client,
        )
        try:
            await provider.chat([], [], EventBus(), "run-3")
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected RuntimeError")

    assert "HTTP 401" in message
    assert "secret-key" not in message

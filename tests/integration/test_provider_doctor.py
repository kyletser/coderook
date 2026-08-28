from __future__ import annotations

import json

import httpx
import pytest
from pydantic import AnyHttpUrl

from code_rook.core.llm.credentials import CredentialResolution
from code_rook.core.llm.doctor import ProviderDoctor
from code_rook.core.llm.routes import ProviderRoute

_TOOL_ONE = "coderook_doctor_echo_one"
_TOOL_TWO = "coderook_doctor_echo_two"


# 构造默认只声明流式文本能力的隔离测试 route
def _route(
    wire_format: str = "openai_chat",
    **updates: object,
) -> ProviderRoute:
    return ProviderRoute.model_validate(
        {
            "id": "doctor-route",
            "provider": "openai-compatible",
            "wire_format": wire_format,
            "base_url": AnyHttpUrl(
                "https://provider.example"
                if wire_format == "anthropic_messages"
                else "https://provider.example/v1/probe"
            ),
            "model": "model-x",
            "credential_ref": "file:doctor-route",
            "supports_tools": False,
            "supports_parallel_tools": False,
            **updates,
        }
    )


# 返回三种 wire format 的正常流式终止响应
def _normal_sse(wire_format: str) -> str:
    if wire_format == "openai_chat":
        return "\n".join(
            (
                'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
                "",
            )
        )
    if wire_format == "openai_responses":
        return "\n".join(
            (
                'data: {"type":"response.output_text.delta","delta":"OK"}',
                'data: {"type":"response.completed","response":{"status":"completed","output":[]}}',
                "",
            )
        )
    return "\n".join(
        (
            'data: {"type":"message_start","message":{"content":[]}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
            'data: {"type":"message_stop"}',
            "",
        )
    )


# 返回三种 wire format 的完整工具调用终态流
def _tool_sse(wire_format: str, names: tuple[str, ...]) -> str:
    if wire_format == "openai_chat":
        calls = [
            {
                "index": index,
                "id": f"call-{index}",
                "function": {"name": name, "arguments": '{"value":"doctor"}'},
            }
            for index, name in enumerate(names)
        ]
        return "\n".join(
            (
                f'data: {json.dumps({"choices": [{"delta": {"tool_calls": calls}, "finish_reason": None}]})}',
                'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
                "",
            )
        )
    if wire_format == "openai_responses":
        output = [
            {
                "type": "function_call",
                "id": f"item-{index}",
                "call_id": f"call-{index}",
                "name": name,
                "arguments": '{"value":"doctor"}',
            }
            for index, name in enumerate(names)
        ]
        return "\n".join(
            (
                f'data: {json.dumps({"type": "response.completed", "response": {"status": "completed", "output": output}})}',
                "",
            )
        )
    rows = [
        f'data: {json.dumps({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": f"tool-{index}", "name": name, "input": {"value": "doctor"}}})}'
        for index, name in enumerate(names)
    ]
    rows.extend(
        (
            'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}',
            'data: {"type":"message_stop"}',
            "",
        )
    )
    return "\n".join(rows)


# 功能：验证 Doctor 对三种 wire format 真实消费流并要求正常终止
# 设计：参数化协议流和请求关键字段，MockTransport 同时断言密钥只在 header 中
@pytest.mark.parametrize(
    ("wire_format", "request_key"),
    [
        ("openai_chat", "messages"),
        ("openai_responses", "input"),
        ("anthropic_messages", "messages"),
    ],
)
async def test_provider_doctor_accepts_wire_specific_success(
    wire_format: str,
    request_key: str,
) -> None:
    captured: list[httpx.Request] = []

    # 捕获探测请求并返回参数化成功结构
    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text=_normal_sse(wire_format),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            _route(wire_format),
            CredentialResolution(value="doctor-secret", source="file"),
        )

    payload = json.loads(captured[0].content)
    assert request_key in payload
    assert payload["stream"] is True
    assert result.status == "ok"
    assert result.readiness == "verified"
    assert result.basic.status == "passed"
    assert result.capabilities["streaming"].status == "passed"
    assert result.capabilities["termination"].status == "passed"
    assert result.capabilities["tool_calling"].status == "unsupported"
    assert result.route_digest == _route(wire_format).validation_digest()
    assert result.credential_source == "file"
    assert "doctor-secret" not in result.model_dump_json()
    if wire_format == "anthropic_messages":
        assert captured[0].url.path == "/v1/messages"


# 功能：验证三种 wire format 声明并行工具时必须在单个终态流中返回两个调用
# 设计：根据请求是否含 tools 返回正常或双工具 SSE，同时断言并行探针兼作普通工具证据
@pytest.mark.parametrize(
    "wire_format",
    ["openai_chat", "openai_responses", "anthropic_messages"],
)
async def test_provider_doctor_probes_declared_parallel_tools(
    wire_format: str,
) -> None:
    requests: list[dict[str, object]] = []

    # 为基础探针返回正常终态，为工具探针返回两个完整调用
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        body = (
            _tool_sse(wire_format, (_TOOL_ONE, _TOOL_TWO))
            if "tools" in payload
            else _normal_sse(wire_format)
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    route = _route(
        wire_format,
        supports_tools=True,
        supports_parallel_tools=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            route,
            CredentialResolution(value="doctor-secret", source="file"),
        )

    assert len(requests) == 2
    assert result.status == "ok"
    assert result.capabilities["tool_calling"].status == "passed"
    assert result.capabilities["parallel_tools"].status == "passed"
    assert result.to_receipt(route).route_digest == route.validation_digest()


# 功能：验证普通 OpenAI 兼容工具探测不发送仅部分 Provider 支持的并行控制字段
# 设计：捕获基础与工具请求并返回真实 SSE 形状，锁定 Doctor 与运行时一致的 auto 工具选择和有界余量
async def test_provider_doctor_uses_minimal_openai_tool_probe() -> None:
    requests: list[dict[str, object]] = []

    # 根据请求是否携带工具返回对应的完整流式终态
    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        body = (
            _tool_sse("openai_chat", (_TOOL_ONE,))
            if "tools" in payload
            else _normal_sse("openai_chat")
        )
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    route = _route(supports_tools=True, supports_parallel_tools=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            route,
            CredentialResolution(value="doctor-secret", source="file"),
        )

    assert result.status == "ok"
    assert requests[0]["max_tokens"] == 64
    assert requests[1]["max_tokens"] == 128
    assert requests[1]["tool_choice"] == "auto"
    assert "parallel_tool_calls" not in requests[1]


# 功能：验证 route 声明图片能力时三种协议都发送微型内存图片并要求正常终态
# 设计：两次请求均返回正常 SSE，再检查第二次请求含 data URI 或 base64 image block
@pytest.mark.parametrize(
    "wire_format",
    ["openai_chat", "openai_responses", "anthropic_messages"],
)
async def test_provider_doctor_probes_declared_images(wire_format: str) -> None:
    request_bodies: list[str] = []

    # 记录脱敏请求形状并返回正常完成流
    async def respond(request: httpx.Request) -> httpx.Response:
        request_bodies.append(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text=_normal_sse(wire_format),
        )

    route = _route(wire_format, supports_images=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            route,
            CredentialResolution(value="doctor-secret", source="file"),
        )

    assert len(request_bodies) == 2
    assert "image" in request_bodies[1]
    assert result.status == "ok"
    assert result.capabilities["images"].status == "passed"


# 功能：验证缺失正常终止或非流式响应都不能生成可提交收据
# 设计：参数化 SSE 提前 EOF 与兼容 JSON，分别锁定 termination 和 streaming 分类
@pytest.mark.parametrize(
    ("headers", "body", "category"),
    [
        (
            {"content-type": "text/event-stream"},
            'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n',
            "termination",
        ),
        (
            {"content-type": "application/json"},
            '{"choices":[{"message":{"content":"OK"},"finish_reason":"stop"}]}',
            "streaming",
        ),
    ],
)
async def test_provider_doctor_rejects_incomplete_or_non_streaming_success(
    headers: dict[str, str],
    body: str,
    category: str,
) -> None:
    # 返回参数化的伪成功响应
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, headers=headers, text=body)

    route = _route()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            route,
            CredentialResolution(value="doctor-secret", source="file"),
        )

    assert result.status == "error"
    assert result.category == category
    with pytest.raises(ValueError, match="not valid"):
        result.to_receipt(route)


# 功能：验证 Doctor 分类 credential、model 与 schema HTTP 错误且不回显正文
# 设计：参数化状态码和带 secret 的响应体，检查分类与结果序列化脱敏
@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (401, "invalid doctor-secret", "credential"),
        (400, "model model-x not found doctor-secret", "model"),
        (422, "invalid request schema doctor-secret", "schema"),
    ],
)
async def test_provider_doctor_classifies_http_failures_without_body(
    status: int,
    body: str,
    category: str,
) -> None:
    # 返回参数化 Provider 错误
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, text=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            _route(),
            CredentialResolution(value="doctor-secret", source="env"),
        )

    assert result.category == category
    assert result.http_status == status
    assert "doctor-secret" not in result.model_dump_json()
    assert body not in result.model_dump_json()


# 功能：验证缺失 credential 在发起网络请求前直接报告
# 设计：transport 若被调用即失败，证明 missing path 不可能泄露或访问 endpoint
async def test_provider_doctor_reports_missing_credential_without_request() -> None:
    # 禁止任何外部请求
    async def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("request must not be sent")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            _route(),
            CredentialResolution(source="missing"),
        )

    assert result.category == "credential"
    assert result.credential_source == "missing"


# 功能：验证免密本地 route 的 Doctor 可探测成功且不会发送空 Bearer 头
# 设计：用 MockTransport 接住 loopback 请求并返回 choices，避免测试依赖真实 Ollama 进程
async def test_provider_doctor_accepts_credential_free_local_route() -> None:
    captured: list[httpx.Request] = []

    # 记录免密请求并返回最小 Chat Completions 结构
    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            text=_normal_sse("openai_chat"),
        )

    route = ProviderRoute(
        id="local-doctor",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("http://127.0.0.1:11434/v1/chat/completions"),
        model="qwen3-coder",
        credential_ref="none:ollama",
        catalog_id="ollama",
        credential_required=False,
        supports_tools=False,
        supports_parallel_tools=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            route,
            CredentialResolution(source="missing"),
        )

    assert result.status == "ok"
    assert "Authorization" not in captured[0].headers


# 功能：验证 TLS 与普通网络故障得到不同分类
# 设计：参数化 MockTransport 抛出的 ConnectError 文本，覆盖证书标记与普通拒绝连接
@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("SSL CERTIFICATE_VERIFY_FAILED", "tls"),
        ("connection refused", "network"),
    ],
)
async def test_provider_doctor_classifies_transport_errors(
    message: str,
    category: str,
) -> None:
    # 抛出参数化连接错误
    async def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(message, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            _route(),
            CredentialResolution(value="doctor-secret", source="keyring"),
        )

    assert result.category == category
    assert "doctor-secret" not in result.model_dump_json()


# 功能：验证成功 HTTP 返回错误 JSON 形状时归类 schema
# 设计：返回 200 但缺少 choices，区分传输成功和 wire contract 成功
async def test_provider_doctor_rejects_incompatible_success_schema() -> None:
    # 返回与 Chat Completions 不兼容的对象
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={"unexpected": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            _route(),
            CredentialResolution(value="doctor-secret", source="file"),
        )

    assert result.category == "schema"
    assert result.http_status == 200

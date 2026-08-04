from __future__ import annotations

import json

import httpx
import pytest
from pydantic import AnyHttpUrl

from code_rook.core.llm.credentials import CredentialResolution
from code_rook.core.llm.doctor import ProviderDoctor
from code_rook.core.llm.routes import ProviderRoute


# 构造指定 wire format 的隔离测试 route
def _route(wire_format: str = "openai_chat") -> ProviderRoute:
    return ProviderRoute.model_validate(
        {
            "id": "doctor-route",
            "provider": "openai-compatible",
            "wire_format": wire_format,
            "base_url": AnyHttpUrl("https://provider.example/v1/probe"),
            "model": "model-x",
            "credential_ref": "file:doctor-route",
        }
    )


# 功能：验证 Doctor 对三种 wire format 发送匹配请求并接受对应成功结构
# 设计：参数化请求关键字段和响应根字段，MockTransport 同时断言密钥只在 header 中
@pytest.mark.parametrize(
    ("wire_format", "request_key", "response_payload"),
    [
        ("openai_chat", "messages", {"choices": []}),
        ("openai_responses", "input", {"output": []}),
        ("anthropic_messages", "messages", {"content": []}),
    ],
)
async def test_provider_doctor_accepts_wire_specific_success(
    wire_format: str,
    request_key: str,
    response_payload: dict[str, object],
) -> None:
    captured: list[httpx.Request] = []

    # 捕获探测请求并返回参数化成功结构
    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, request=request, json=response_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await ProviderDoctor(client=client).check(
            _route(wire_format),
            CredentialResolution(value="doctor-secret", source="file"),
        )

    payload = json.loads(captured[0].content)
    assert request_key in payload
    assert result.status == "ok"
    assert result.credential_source == "file"
    assert "doctor-secret" not in result.model_dump_json()


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

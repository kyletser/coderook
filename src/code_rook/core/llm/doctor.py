from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict

from code_rook.core.llm.credentials import CredentialResolution
from code_rook.core.llm.routes import CredentialSource, ProviderRoute

DoctorCategory = Literal["ok", "credential", "tls", "schema", "model", "network"]


class ProviderDoctorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "error"]
    category: DoctorCategory
    route_id: str
    message: str
    credential_source: CredentialSource
    http_status: int | None = None


# 判断异常链是否表明 TLS 握手或证书校验失败
def _is_tls_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        text = f"{type(current).__name__} {current}".casefold()
        if any(marker in text for marker in ("ssl", "tls", "certificate")):
            return True
        current = current.__cause__ or current.__context__
    return False


# 生成与 route wire format 一致的最小探测请求
def _probe_request(
    route: ProviderRoute,
    credential: str,
) -> tuple[dict[str, str], dict[str, object]]:
    if route.wire_format == "anthropic_messages":
        return (
            {
                "x-api-key": credential,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            {
                "model": route.model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            },
        )
    headers = {
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json",
    }
    if route.wire_format == "openai_responses":
        return (
            headers,
            {
                "model": route.model,
                "input": "ping",
                "max_output_tokens": 1,
                "store": False,
            },
        )
    return (
        headers,
        {
            "model": route.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
    )


# 判断成功响应是否具有所选 wire format 的最小结构
def _valid_response_shape(route: ProviderRoute, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if route.wire_format == "anthropic_messages":
        return isinstance(payload.get("content"), list)
    if route.wire_format == "openai_responses":
        return isinstance(payload.get("output"), list)
    return isinstance(payload.get("choices"), list)


# 将 HTTP 错误分类为 credential、model 或 schema，不保留响应正文
def _http_failure(route: ProviderRoute, response: httpx.Response) -> ProviderDoctorResult:
    status = response.status_code
    body = response.text[:2_000].casefold()
    if status in {401, 403}:
        category: DoctorCategory = "credential"
        message = "credential was rejected by the provider"
    elif "model" in body and any(
        marker in body for marker in ("not found", "unknown", "invalid", "does not exist")
    ):
        category = "model"
        message = "configured model was rejected by the provider"
    else:
        category = "schema"
        message = "provider rejected the route request schema or endpoint"
    return ProviderDoctorResult(
        status="error",
        category=category,
        route_id=route.id,
        message=message,
        credential_source="missing",
        http_status=status,
    )


class ProviderDoctor:
    # 初始化可注入 HTTP client 的 route 诊断器
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    # 探测 route 并返回不含密钥、响应正文和异常细节的分类结果
    async def check(
        self,
        route: ProviderRoute,
        credential: CredentialResolution,
    ) -> ProviderDoctorResult:
        if credential.value is None:
            return ProviderDoctorResult(
                status="error",
                category="credential",
                route_id=route.id,
                message="credential is missing",
                credential_source="missing",
            )
        headers, payload = _probe_request(route, credential.value)
        try:
            if self._client is None:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    response = await client.post(
                        str(route.base_url),
                        headers=headers,
                        json=payload,
                    )
            else:
                response = await self._client.post(
                    str(route.base_url),
                    headers=headers,
                    json=payload,
                )
        except httpx.HTTPError as exc:
            category: DoctorCategory = "tls" if _is_tls_error(exc) else "network"
            message = (
                "TLS handshake or certificate validation failed"
                if category == "tls"
                else "provider endpoint could not be reached"
            )
            return ProviderDoctorResult(
                status="error",
                category=category,
                route_id=route.id,
                message=message,
                credential_source=credential.source,
            )
        if response.is_error:
            failure = _http_failure(route, response)
            return failure.model_copy(
                update={"credential_source": credential.source}
            )
        try:
            response_payload = response.json()
        except ValueError:
            response_payload = None
        if not _valid_response_shape(route, response_payload):
            return ProviderDoctorResult(
                status="error",
                category="schema",
                route_id=route.id,
                message="provider returned an incompatible response schema",
                credential_source=credential.source,
                http_status=response.status_code,
            )
        return ProviderDoctorResult(
            status="ok",
            category="ok",
            route_id=route.id,
            message="route is ready",
            credential_source=credential.source,
            http_status=response.status_code,
        )

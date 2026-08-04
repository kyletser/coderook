from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

ProviderKind = Literal[
    "anthropic",
    "openai",
    "openai-compatible",
    "anthropic-compatible",
    "opencode-zen",
]
WireFormat = Literal["openai_chat", "openai_responses", "anthropic_messages"]
CredentialSource = Literal["keyring", "file", "env", "missing"]


# 判断主机名是否只指向本机，供明文 HTTP 安全校验使用
def _is_loopback_host(host: str) -> bool:
    normalized = host.strip("[]").rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


class RouteReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str
    wire_format: WireFormat
    base_url_origin: str
    model: str
    credential_source: CredentialSource


class ProviderRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    provider: ProviderKind
    wire_format: WireFormat
    base_url: AnyHttpUrl
    model: str = Field(min_length=1)
    credential_ref: str = Field(min_length=1)
    context_window: int | None = Field(default=None, ge=1)
    supports_tools: bool = True
    supports_parallel_tools: bool = True
    supports_prompt_cache: bool = False

    @model_validator(mode="after")
    # 拒绝 URL 内嵌凭据及非 loopback 明文 HTTP，防止密钥被发送到不安全端点
    def _validate_endpoint_security(self) -> ProviderRoute:
        parsed = urlsplit(str(self.base_url))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain embedded credentials")
        host = self.base_url.host or ""
        if self.base_url.scheme == "http" and not _is_loopback_host(host):
            raise ValueError("plain HTTP is only allowed for loopback endpoints")
        return self

    # 生成不含密钥和 URL 路径的实际路由收据
    def receipt(self, credential_source: CredentialSource) -> RouteReceipt:
        parsed = urlsplit(str(self.base_url))
        origin = f"{parsed.scheme}://{parsed.hostname or ''}"
        if parsed.port is not None:
            origin += f":{parsed.port}"
        return RouteReceipt(
            route_id=self.id,
            wire_format=self.wire_format,
            base_url_origin=origin,
            model=self.model,
            credential_source=credential_source,
        )


_PRESET_ROUTES: dict[str, ProviderRoute] = {
    "anthropic": ProviderRoute(
        id="anthropic",
        provider="anthropic",
        wire_format="anthropic_messages",
        base_url=AnyHttpUrl("https://api.anthropic.com"),
        model="claude-sonnet-4-6",
        credential_ref="env:ANTHROPIC_API_KEY",
        supports_prompt_cache=True,
    ),
    "openai": ProviderRoute(
        id="openai",
        provider="openai",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("https://api.openai.com/v1/chat/completions"),
        model="gpt-5.6-terra",
        credential_ref="env:OPENAI_API_KEY",
    ),
    "openai-compatible": ProviderRoute(
        id="openai-compatible",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("http://127.0.0.1:11434/v1/chat/completions"),
        model="local-model",
        credential_ref="env:OPENAI_API_KEY",
    ),
    "anthropic-compatible": ProviderRoute(
        id="anthropic-compatible",
        provider="anthropic-compatible",
        wire_format="anthropic_messages",
        base_url=AnyHttpUrl("http://127.0.0.1:8080"),
        model="local-model",
        credential_ref="env:ANTHROPIC_API_KEY",
        supports_prompt_cache=True,
    ),
    "opencode-zen": ProviderRoute(
        id="opencode-zen",
        provider="opencode-zen",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("https://opencode.ai/zen/v1/chat/completions"),
        model="deepseek-v4-flash",
        credential_ref="env:OPENCODE_API_KEY",
    ),
}


# 返回独立的内置路由副本，可覆盖模型但不能静默改变 wire format
def get_route_preset(route_id: str, *, model: str | None = None) -> ProviderRoute:
    try:
        route = _PRESET_ROUTES[route_id]
    except KeyError as exc:
        raise ValueError(f"unknown route preset: {route_id}") from exc
    if model is None:
        return route.model_copy(deep=True)
    selected = model.strip()
    if not selected:
        raise ValueError("model cannot be empty")
    return route.model_copy(update={"model": selected}, deep=True)


# 返回所有内置路由，保持用户界面展示顺序稳定
def list_route_presets() -> tuple[ProviderRoute, ...]:
    return tuple(route.model_copy(deep=True) for route in _PRESET_ROUTES.values())

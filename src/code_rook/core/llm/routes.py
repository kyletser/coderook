from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

from code_rook.core.llm.provider_presets import PROVIDER_PRESETS, ProviderPreset

ProviderKind = Literal[
    "anthropic",
    "openai",
    "openai-compatible",
    "anthropic-compatible",
    "opencode-zen",
]
WireFormat = Literal["openai_chat", "openai_responses", "anthropic_messages"]
CredentialSource = Literal["keyring", "file", "env", "missing"]
# 推理预算档位：off 关闭；low/medium/high 由各 wire format 映射为原生参数
ThinkingLevel = Literal["off", "low", "medium", "high"]
DoctorCheckStatus = Literal["passed", "failed", "not_run", "unsupported"]


class DoctorCapabilityReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DoctorCheckStatus
    message: str


class RouteDoctorReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checked_at: str
    basic: DoctorCapabilityReceipt
    capabilities: dict[str, DoctorCapabilityReceipt] = Field(default_factory=dict)


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
    supports_images: bool = False
    temperature: float | None = None


class ProviderRoute(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    provider: ProviderKind
    wire_format: WireFormat
    base_url: AnyHttpUrl
    model: str = Field(min_length=1)
    credential_ref: str = Field(min_length=1)
    catalog_id: str | None = None
    credential_required: bool = True
    context_window: int | None = Field(default=None, ge=1)
    supports_tools: bool = True
    supports_parallel_tools: bool = True
    supports_prompt_cache: bool = False
    supports_images: bool = False
    # 推理预算档位；off 表示不请求 extended thinking / reasoning effort
    thinking: ThinkingLevel = "off"
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    doctor_receipt: RouteDoctorReceipt | None = None

    @model_validator(mode="before")
    @classmethod
    # 兼容用户输入标准 OpenAI base URL，只在路径以 /v1 结尾时补全 Chat endpoint
    def _normalize_openai_chat_base_url(cls, value: object) -> object:
        if not isinstance(value, dict) or value.get("wire_format") != "openai_chat":
            return value
        raw_url = value.get("base_url")
        if raw_url is None:
            return value
        parsed = urlsplit(str(raw_url).strip())
        path = parsed.path.rstrip("/")
        if not path.endswith("/v1"):
            return value
        payload = dict(value)
        payload["base_url"] = urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"{path}/chat/completions",
                parsed.query,
                parsed.fragment,
            )
        )
        return payload

    @model_validator(mode="after")
    # 拒绝 URL 内嵌凭据及非 loopback 明文 HTTP，防止密钥被发送到不安全端点
    def _validate_endpoint_security(self) -> ProviderRoute:
        parsed = urlsplit(str(self.base_url))
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("base_url must not contain embedded credentials")
        host = self.base_url.host or ""
        if self.base_url.scheme == "http" and not _is_loopback_host(host):
            raise ValueError("plain HTTP is only allowed for loopback endpoints")
        if not self.credential_required and not _is_loopback_host(host):
            raise ValueError("credential-free routes are only allowed on loopback endpoints")
        if self.supports_parallel_tools and not self.supports_tools:
            raise ValueError("parallel tools require tool calling support")
        if self.wire_format == "anthropic_messages" and self.temperature is not None:
            if self.temperature > 1.0:
                raise ValueError("Anthropic temperature must be between 0 and 1")
            if self.thinking != "off" and self.temperature != 1.0:
                raise ValueError("Anthropic extended thinking requires temperature=1")
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
            supports_images=self.supports_images,
            temperature=self.temperature,
        )

    # 计算不含凭据正文与 Doctor 收据的稳定执行路由摘要
    def validation_digest(self) -> str:
        payload = self.model_dump(
            mode="json",
            exclude={"credential_ref", "doctor_receipt"},
        )
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # 判断持久 Doctor 收据是否仍精确绑定当前 route 与 model
    def has_current_doctor_receipt(self) -> bool:
        receipt = self.doctor_receipt
        required = ["streaming", "termination"]
        if self.supports_tools:
            required.append("tool_calling")
        if self.supports_parallel_tools:
            required.append("parallel_tools")
        if self.supports_images:
            required.append("images")
        return bool(
            receipt is not None
            and receipt.route_digest == self.validation_digest()
            and receipt.basic.status == "passed"
            and all(
                name in receipt.capabilities
                and receipt.capabilities[name].status == "passed"
                for name in required
            )
        )


# 从统一 Provider catalog 构造带来源和免密语义的显式 route
def _route_from_provider_preset(preset: ProviderPreset) -> ProviderRoute:
    return ProviderRoute.model_validate(
        {
            "id": preset.id,
            "provider": preset.provider_kind,
            "wire_format": preset.wire_format,
            "base_url": preset.chat_url,
            "model": preset.default_model,
            "credential_ref": (
                f"env:{preset.api_key_env}" if preset.credential_required else f"none:{preset.id}"
            ),
            "catalog_id": preset.id,
            "credential_required": preset.credential_required,
            "supports_tools": preset.supports_tools,
            "supports_parallel_tools": preset.supports_parallel_tools,
            "supports_prompt_cache": preset.supports_prompt_cache,
            "supports_images": preset.supports_images,
        }
    )


_PRESET_ROUTES: dict[str, ProviderRoute] = {
    preset.id: _route_from_provider_preset(preset) for preset in PROVIDER_PRESETS
}
_PRESET_ROUTES.update(
    {
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
)


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

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pydantic import AnyHttpUrl, ValidationError

from code_rook.core.config import LlmConfig
from code_rook.core.llm.credentials import (
    CredentialResolution,
    CredentialStore,
    normalize_provider,
)
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.llm.routes import ProviderRoute, RouteReceipt


class RouteResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedRoute:
    route: ProviderRoute
    receipt: RouteReceipt
    credential: str = field(repr=False)


# 将旧 LlmConfig 显式映射成 route，不依赖模型名称前缀
def legacy_config_route(config: LlmConfig) -> ProviderRoute:
    provider = normalize_provider(config.provider)
    if provider == "anthropic":
        provider_kind = "anthropic"
        wire_format = "anthropic_messages"
        base_url = config.base_url or "https://api.anthropic.com"
    elif provider in {"deepseek", "openai", "openai_compatible", "siliconflow"}:
        provider_kind = "openai" if provider == "openai" else "openai-compatible"
        wire_format = "openai_chat"
        base_url = config.base_url
    else:
        raise RouteResolutionError(f"unsupported legacy provider: {config.provider}")
    if not base_url:
        raise RouteResolutionError(f"route endpoint is missing for provider: {config.provider}")
    credential_ref = (
        f"env:{config.api_key_env}"
        if config.api_key_env and os.environ.get(config.api_key_env)
        else f"file:{provider}"
    )
    try:
        return ProviderRoute(
            id=f"legacy-{provider.replace('_', '-')}",
            provider=provider_kind,  # type: ignore[arg-type]
            wire_format=wire_format,  # type: ignore[arg-type]
            base_url=AnyHttpUrl(base_url),
            model=config.default_model,
            credential_ref=credential_ref,
            supports_prompt_cache=wire_format == "anthropic_messages",
        )
    except ValidationError as exc:
        raise RouteResolutionError(f"invalid legacy route for provider: {provider}") from exc


class RouteRegistry:
    # 初始化 route store、credential store 和旧配置兼容入口
    def __init__(
        self,
        config: LlmConfig,
        *,
        route_store: RouteStore | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self._config = config
        self._routes = route_store or RouteStore()
        self._credentials = credential_store or CredentialStore()

    # 返回指定或活动 route；尚未迁移时生成不落盘的旧配置 route
    def route(self, route_id: str | None = None) -> ProviderRoute:
        if route_id is not None:
            try:
                return self._routes.get(route_id)
            except RouteStoreError as exc:
                raise RouteResolutionError(
                    f"profile route is not configured: {route_id}"
                ) from exc
        active = self._routes.active()
        return active if active is not None else legacy_config_route(self._config)

    # 解析 route 凭据并生成不含敏感正文的冻结收据
    def resolve(self, route_id: str | None = None) -> ResolvedRoute:
        route = self.route(route_id)
        credential: CredentialResolution = self._credentials.resolve(
            route.credential_ref
        )
        if credential.value is None:
            raise RouteResolutionError(f"credential is missing for route: {route.id}")
        return ResolvedRoute(
            route=route,
            receipt=route.receipt(credential.source),
            credential=credential.value,
        )

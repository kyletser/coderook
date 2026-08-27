from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import AnyHttpUrl, ValidationError

from code_rook.core.config import LlmConfig
from code_rook.core.configuration import ConfigurationService
from code_rook.core.llm.credentials import (
    CredentialResolution,
    CredentialStore,
    normalize_provider,
    resolve_env_credential,
)
from code_rook.core.llm.kinds import OPENAI_CHAT_PROVIDERS
from code_rook.core.llm.migration_receipt import (
    ProviderCatalogMigrationReceiptStore,
    build_provider_catalog_migration_receipt,
)
from code_rook.core.llm.provider_presets import get_provider_preset
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
    catalog_id: str | None = None
    credential_required = True
    try:
        preset = get_provider_preset(provider)
        catalog_id = preset.id
        credential_required = preset.credential_required
    except ValueError:
        preset = None
    if provider == "anthropic":
        provider_kind = "anthropic"
        wire_format = "anthropic_messages"
        base_url = config.base_url or "https://api.anthropic.com"
    elif provider in OPENAI_CHAT_PROVIDERS:
        provider_kind = "openai" if provider == "openai" else "openai-compatible"
        wire_format = "openai_chat"
        base_url = config.base_url
    else:
        raise RouteResolutionError(f"unsupported legacy provider: {config.provider}")
    if not base_url:
        raise RouteResolutionError(f"route endpoint is missing for provider: {config.provider}")
    if not credential_required:
        credential_ref = f"none:{catalog_id or provider}"
    else:
        credential_ref = (
            f"env:{config.api_key_env}"
            if config.api_key_env
            and resolve_env_credential(
                config.api_key_env,
                config.credential_overlay,
            )
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
            catalog_id=catalog_id,
            credential_required=credential_required,
            supports_prompt_cache=(
                preset.supports_prompt_cache
                if preset is not None
                else wire_format == "anthropic_messages"
            ),
            supports_images=(
                preset.supports_images
                if preset is not None
                else provider in {"anthropic", "openai"}
            ),
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
        migration_receipt_store: ProviderCatalogMigrationReceiptStore | None = None,
        temperature_override: float | None = None,
    ) -> None:
        self._config = config
        self._routes = route_store or RouteStore()
        self._credentials = credential_store or CredentialStore(
            env_overlay=config.credential_overlay
        )
        self._migration_receipts = (
            migration_receipt_store
            or ProviderCatalogMigrationReceiptStore(self._routes.path.parent)
        )
        self._temperature_override = temperature_override

    # 返回复用同一 Route 与凭据存储的配置事务服务，供本地多前端统一管理模型
    def configuration_service(self) -> ConfigurationService:
        return ConfigurationService(self._routes, self._credentials)

    # 迁移旧 LlmConfig 或确认无需迁移，并写入绑定输入输出的完成收据
    def migrate_legacy_config(
        self,
        *,
        legacy_configured: bool = True,
    ) -> ProviderRoute | None:
        with self._routes.transaction():
            return self._migrate_legacy_config_locked(
                legacy_configured=legacy_configured
            )

    # 在 Route Catalog 跨进程事务内完成检查、写路由和迁移收据
    def _migrate_legacy_config_locked(
        self,
        *,
        legacy_configured: bool,
    ) -> ProviderRoute | None:
        receipt_status = self._migration_receipts.inspect()
        if receipt_status == "invalid":
            raise RouteResolutionError(
                "provider catalog migration receipt is invalid; refusing migration"
            )
        existing_receipt = (
            self._migration_receipts.load()
            if receipt_status == "complete"
            else None
        )
        configured = self._routes.list()
        if existing_receipt is not None:
            if configured:
                return None
            if (
                existing_receipt.outcome == "legacy_not_configured"
                and not legacy_configured
            ):
                return None
            raise RouteResolutionError(
                "provider catalog migration is complete but the Route Catalog is empty; "
                "refusing automatic remigration"
            )
        if configured:
            receipt = build_provider_catalog_migration_receipt(
                self._config,
                self._routes,
                outcome="catalog_present",
            )
            self._migration_receipts.write(receipt)
            return None
        if not legacy_configured:
            receipt = build_provider_catalog_migration_receipt(
                self._config,
                self._routes,
                outcome="legacy_not_configured",
            )
            self._migration_receipts.write(receipt)
            return None
        route = legacy_config_route(self._config)
        snapshot = self._routes.snapshot()
        try:
            self._routes.add(route, activate=True)
            receipt = build_provider_catalog_migration_receipt(
                self._config,
                self._routes,
                outcome="migrated",
            )
            self._migration_receipts.write(receipt)
        except BaseException:
            self._routes.restore(snapshot)
            raise
        return route

    # 返回指定或活动 route；尚未迁移时生成不落盘的旧配置 route
    def route(self, route_id: str | None = None) -> ProviderRoute:
        if route_id is not None:
            try:
                route = self._routes.get(route_id)
            except RouteStoreError as exc:
                raise RouteResolutionError(f"profile route is not configured: {route_id}") from exc
        else:
            active = self._routes.active()
            if active is None:
                inspection = self._routes.inspect()
                if inspection.active_route_unavailable:
                    raise RouteResolutionError(
                        "the configured active provider route is invalid or unavailable"
                    )
            route = active if active is not None else legacy_config_route(self._config)
        if self._temperature_override is None:
            return route
        payload = route.model_dump(mode="python")
        payload["temperature"] = self._temperature_override
        return ProviderRoute.model_validate(payload)

    # 返回当前参与路由决策的稳定候选 id，未迁移时包含兼容 route
    def candidate_ids(self) -> list[str]:
        configured = self._routes.list()
        if configured:
            return sorted(route.id for route in configured)
        return [legacy_config_route(self._config).id]

    # 解析 route 凭据并生成不含敏感正文的冻结收据
    def resolve(self, route_id: str | None = None) -> ResolvedRoute:
        route = self.route(route_id)
        credential: CredentialResolution = self._credentials.resolve(route.credential_ref)
        if credential.value is None and route.credential_required:
            raise RouteResolutionError(f"credential is missing for route: {route.id}")
        return ResolvedRoute(
            route=route,
            receipt=route.receipt(credential.source),
            credential=credential.value or "",
        )

    # 只接受已通过统一 readiness 的 route，并可绑定模型及原 Worker 摘要
    async def resolve_ready(
        self,
        route_id: str | None = None,
        *,
        model: str | None = None,
        expected_digest: str = "",
    ) -> ResolvedRoute:
        route = self.route(route_id)
        if model is not None and model != route.model:
            route = route.model_copy(update={"model": model})
        digest = route.validation_digest()
        if expected_digest and expected_digest != digest:
            raise RouteResolutionError(
                f"route changed since worker creation: {route.id}"
            )
        configuration = ConfigurationService(self._routes, self._credentials)
        readiness = configuration.readiness(route)
        if not readiness.local_ready:
            raise RouteResolutionError(
                f"route is not ready: {route.id} ({readiness.status})"
            )
        probed = await configuration.probe_readiness(route)
        if not probed.local_ready:
            raise RouteResolutionError(
                f"route is unavailable: {route.id} ({probed.status})"
            )
        credential = self._credentials.resolve(route.credential_ref)
        if credential.value is None and route.credential_required:
            raise RouteResolutionError(f"credential is missing for route: {route.id}")
        return ResolvedRoute(
            route=route,
            receipt=route.receipt(credential.source),
            credential=credential.value or "",
        )

from __future__ import annotations

import asyncio
import getpass
from collections.abc import Callable, Mapping
from pathlib import Path

from code_rook.core.config import CodeRookConfig
from code_rook.core.configuration import ConfigurationService
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctor
from code_rook.core.llm.provider_presets import get_provider_preset
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import ProviderRoute, get_route_preset
from code_rook.core.upgrade import v1_state_mutation


# 返回显式注入的凭据存储，或从当前配置构造携带 env overlay 的默认存储
def _configured_credentials(
    config: CodeRookConfig | None,
    credential_store: CredentialStore | None,
) -> CredentialStore:
    if credential_store is not None:
        return credential_store
    return CredentialStore(
        env_overlay=config.llm.credential_overlay if config is not None else None
    )


# 使用完整字典重新校验 route 更新，避免 model_copy 跳过安全校验
def _updated_route(route: ProviderRoute, updates: Mapping[str, object]) -> ProviderRoute:
    payload = route.model_dump(mode="python")
    payload.update(updates)
    return ProviderRoute.model_validate(payload)


# 为默认用户存储或测试注入存储选择同一短时状态互斥根目录
def _mutation_root(
    state_root: Path | None,
    route_store: RouteStore | None,
    routes: RouteStore,
) -> Path | None:
    if state_root is not None:
        return state_root
    if route_store is not None:
        return routes.path.parent
    return None


# 打印已配置路由及其活动状态和凭据来源，不显示密钥正文
def cmd_provider_list(
    config: CodeRookConfig,
    *,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
) -> None:
    configuration = ConfigurationService(
        route_store or RouteStore(),
        _configured_credentials(config, credential_store),
    )
    snapshot = configuration.snapshot()
    for issue in snapshot.route_issues:
        index = "-" if issue.index is None else str(issue.index)
        digest = issue.record_digest[:12] if issue.record_digest is not None else "-"
        print(
            f"! route issue={issue.code} index={index} digest={digest} "
            f"quarantined={issue.quarantined}"
        )
    if not snapshot.routes:
        print(f"no provider route configured  status={snapshot.readiness.status}")
        return
    for route in snapshot.routes:
        marker = "*" if route.id == snapshot.active_route_id else " "
        source = snapshot.credential_sources[route.id]
        catalog = route.catalog_id or "custom"
        try:
            provider_name = get_provider_preset(catalog).name
        except ValueError:
            provider_name = route.provider
        print(
            f"{marker} {route.id}  {provider_name}  {route.wire_format}  "
            f"{route.model}  credential={source}  catalog={catalog}"
        )
    print(
        f"readiness={snapshot.readiness.status}  "
        f"validation={snapshot.readiness.provider_validation}"
    )


# 新增显式 route，可从内置 preset 派生并通过隐藏输入保存独立密钥
def cmd_provider_add(
    route_id: str,
    *,
    preset: str | None,
    provider: str | None,
    wire_format: str | None,
    base_url: str | None,
    model: str | None,
    temperature: float | None = None,
    credential_ref: str | None,
    set_key: bool,
    activate: bool,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    secret_fn: Callable[[str], str] = getpass.getpass,
    validate: bool = False,
    doctor: ProviderDoctor | None = None,
    config: CodeRookConfig | None = None,
    state_root: Path | None = None,
) -> None:
    routes = route_store or RouteStore()
    credentials = _configured_credentials(config, credential_store)
    configuration = ConfigurationService(routes, credentials)
    if preset is not None:
        route = _updated_route(
            get_route_preset(preset),
            {
                "id": route_id,
                **({"provider": provider} if provider is not None else {}),
                **({"wire_format": wire_format} if wire_format is not None else {}),
                **({"base_url": base_url} if base_url is not None else {}),
                **({"model": model} if model is not None else {}),
                **({"temperature": temperature} if temperature is not None else {}),
                **({"credential_ref": credential_ref} if credential_ref is not None else {}),
            },
        )
    else:
        missing = [
            name
            for name, value in (
                ("--provider", provider),
                ("--wire-format", wire_format),
                ("--base-url", base_url),
                ("--model", model),
            )
            if not value
        ]
        if credential_ref is None and not set_key:
            missing.append("--credential-ref or --set-key")
        if missing:
            raise SystemExit(f"missing required route fields: {', '.join(missing)}")
        route = ProviderRoute.model_validate(
            {
                "id": route_id,
                "provider": provider,
                "wire_format": wire_format,
                "base_url": base_url,
                "model": model,
                "credential_ref": credential_ref or f"file:{route_id}",
                "temperature": temperature,
            }
        )
    secret: str | None = None
    if set_key:
        secret = secret_fn("API key: ").strip()
        if not secret:
            raise SystemExit("API key cannot be empty")
    with v1_state_mutation(_mutation_root(state_root, route_store, routes)):
        if validate:
            asyncio.run(
                configuration.save_route_checked(
                    route,
                    secret=secret,
                    activate=activate,
                    doctor=doctor,
                )
            )
        else:
            configuration.save_route(route, secret=secret, activate=activate)
    print(f"route added: {route.id}")


# 编辑现有 route 的显式字段，并可轮换该 route 的独立密钥
def cmd_provider_edit(
    route_id: str,
    *,
    provider: str | None,
    wire_format: str | None,
    base_url: str | None,
    model: str | None,
    temperature: float | None = None,
    credential_ref: str | None,
    set_key: bool,
    activate: bool,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    secret_fn: Callable[[str], str] = getpass.getpass,
    validate: bool = False,
    doctor: ProviderDoctor | None = None,
    config: CodeRookConfig | None = None,
    state_root: Path | None = None,
) -> None:
    routes = route_store or RouteStore()
    credentials = _configured_credentials(config, credential_store)
    configuration = ConfigurationService(routes, credentials)
    updates = {
        key: value
        for key, value in (
            ("provider", provider),
            ("wire_format", wire_format),
            ("base_url", base_url),
            ("model", model),
            ("temperature", temperature),
            ("credential_ref", credential_ref),
        )
        if value is not None
    }
    secret: str | None = None
    if set_key:
        secret = secret_fn("API key: ").strip()
        if not secret:
            raise SystemExit("API key cannot be empty")
    if not updates and not activate and not set_key:
        raise SystemExit("no provider route changes requested")
    with v1_state_mutation(_mutation_root(state_root, route_store, routes)):
        current = routes.get(route_id)
        updated = _updated_route(current, updates) if updates else current
        if validate:
            asyncio.run(
                configuration.save_route_checked(
                    updated,
                    secret=secret,
                    activate=activate,
                    update=True,
                    doctor=doctor,
                )
            )
        else:
            configuration.save_route(
                updated,
                secret=secret,
                activate=activate,
                update=True,
            )
    print(f"route updated: {route_id}")


# 删除 route，并仅在明确请求时删除它引用的独立凭据
def cmd_provider_remove(
    route_id: str,
    *,
    delete_credential: bool,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    state_root: Path | None = None,
) -> None:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    with v1_state_mutation(_mutation_root(state_root, route_store, routes)):
        ConfigurationService(routes, credentials).remove_route(
            route_id,
            delete_credential=delete_credential,
        )
    print(f"route removed: {route_id}")


# 完成全部必需 Doctor 探针后才将 route 设为后续 turn 的活动项
def cmd_provider_use(
    route_id: str,
    *,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    doctor: ProviderDoctor | None = None,
    config: CodeRookConfig | None = None,
    state_root: Path | None = None,
) -> None:
    routes = route_store or RouteStore()
    with v1_state_mutation(_mutation_root(state_root, route_store, routes)):
        route = asyncio.run(
            ConfigurationService(
                routes,
                _configured_credentials(config, credential_store),
            ).set_active_checked(route_id, doctor=doctor)
        )
    print(f"active route: {route.id}")


# 列出 route 绑定的真实模型选择，不通过模型名前缀推断 provider
def cmd_model_list(
    config: CodeRookConfig,
    *,
    route_id: str | None = None,
    route_store: RouteStore | None = None,
) -> None:
    routes = route_store or RouteStore()
    selected: tuple[ProviderRoute, ...]
    if route_id is not None:
        selected = (routes.get(route_id),)
    else:
        selected = routes.list()
        if not selected:
            selected = (RouteRegistry(config.llm, route_store=routes).route(),)
    for route in selected:
        print(f"{route.model}  route={route.id}  wire={route.wire_format}")

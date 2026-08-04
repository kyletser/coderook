from __future__ import annotations

import getpass
from collections.abc import Callable, Mapping

from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import ProviderRoute, get_route_preset


# 使用完整字典重新校验 route 更新，避免 model_copy 跳过安全校验
def _updated_route(route: ProviderRoute, updates: Mapping[str, object]) -> ProviderRoute:
    payload = route.model_dump(mode="python")
    payload.update(updates)
    return ProviderRoute.model_validate(payload)


# 打印已配置路由及其活动状态和凭据来源，不显示密钥正文
def cmd_provider_list(
    config: CodeRookConfig,
    *,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
) -> None:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    configured = routes.list()
    active = routes.active()
    if not configured:
        legacy = RouteRegistry(
            config.llm,
            route_store=routes,
            credential_store=credentials,
        ).route()
        source = credentials.resolve(legacy.credential_ref).source
        print(
            f"* {legacy.id}  {legacy.provider}  {legacy.wire_format}  "
            f"{legacy.model}  credential={source}  legacy"
        )
        return
    for route in configured:
        marker = "*" if active is not None and route.id == active.id else " "
        source = credentials.resolve(route.credential_ref).source
        print(
            f"{marker} {route.id}  {route.provider}  {route.wire_format}  "
            f"{route.model}  credential={source}"
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
    credential_ref: str | None,
    set_key: bool,
    activate: bool,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> None:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    if preset is not None:
        route = _updated_route(
            get_route_preset(preset),
            {
                "id": route_id,
                **({"model": model} if model is not None else {}),
                **(
                    {"credential_ref": credential_ref}
                    if credential_ref is not None
                    else {}
                ),
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
            }
        )
    if set_key:
        secret = secret_fn("API key: ").strip()
        if not secret:
            raise SystemExit("API key cannot be empty")
        route = _updated_route(
            route,
            {"credential_ref": credentials.save(route_id, secret)},
        )
    routes.add(route, activate=activate)
    print(f"route added: {route.id}")


# 编辑现有 route 的显式字段，并可轮换该 route 的独立密钥
def cmd_provider_edit(
    route_id: str,
    *,
    provider: str | None,
    wire_format: str | None,
    base_url: str | None,
    model: str | None,
    credential_ref: str | None,
    set_key: bool,
    activate: bool,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
    secret_fn: Callable[[str], str] = getpass.getpass,
) -> None:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    current = routes.get(route_id)
    updates = {
        key: value
        for key, value in (
            ("provider", provider),
            ("wire_format", wire_format),
            ("base_url", base_url),
            ("model", model),
            ("credential_ref", credential_ref),
        )
        if value is not None
    }
    if set_key:
        secret = secret_fn("API key: ").strip()
        if not secret:
            raise SystemExit("API key cannot be empty")
        updates["credential_ref"] = credentials.save(route_id, secret)
    if not updates and not activate:
        raise SystemExit("no provider route changes requested")
    if updates:
        routes.update(_updated_route(current, updates))
    if activate:
        routes.set_active(route_id)
    print(f"route updated: {route_id}")


# 删除 route，并仅在明确请求时删除它引用的独立凭据
def cmd_provider_remove(
    route_id: str,
    *,
    delete_credential: bool,
    route_store: RouteStore | None = None,
    credential_store: CredentialStore | None = None,
) -> None:
    routes = route_store or RouteStore()
    credentials = credential_store or CredentialStore()
    route = routes.get(route_id)
    routes.remove(route_id)
    if delete_credential:
        credentials.delete(route.credential_ref)
    print(f"route removed: {route_id}")


# 将已配置 route 设为后续 turn 的全局活动路由
def cmd_provider_use(
    route_id: str,
    *,
    route_store: RouteStore | None = None,
) -> None:
    route = (route_store or RouteStore()).set_active(route_id)
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

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from code_rook.core.llm.credentials import CredentialResolution, CredentialStore
from code_rook.core.llm.doctor import ProviderDoctor, ProviderDoctorResult
from code_rook.core.llm.provider_presets import (
    LocalPortConnector,
    get_provider_preset,
    probe_local_provider,
)
from code_rook.core.llm.route_store import RouteStore, RouteStoreError, RouteStoreIssue
from code_rook.core.llm.routes import (
    CredentialSource,
    ProviderKind,
    ProviderRoute,
    WireFormat,
)


class ConfigurationReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "unconfigured",
        "configuration_invalid",
        "credential_missing",
        "configuration_complete",
        "endpoint_unreachable",
        "provider_unverified",
        "provider_verified",
    ]
    local_ready: bool
    route_id: str | None = None
    catalog_id: str | None = None
    provider: ProviderKind | None = None
    wire_format: WireFormat | None = None
    model: str | None = None
    endpoint_origin: str | None = None
    credential_source: CredentialSource = "missing"
    credential_required: bool | None = None
    provider_validation: Literal[
        "not_run",
        "endpoint_reachable",
        "endpoint_unreachable",
        "basic_passed",
        "verified_passed",
        "basic_failed",
        "receipt_stale",
    ] = "not_run"
    reason: str


class ConfigurationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_route_id: str | None
    routes: tuple[ProviderRoute, ...]
    credential_sources: dict[str, CredentialSource]
    readiness: ConfigurationReadiness
    route_issues: tuple[RouteStoreIssue, ...] = ()


class ConfigurationValidationError(RuntimeError):
    # 保存已脱敏的 provider doctor 结果，错误消息不包含凭据或响应正文
    def __init__(self, result: ProviderDoctorResult) -> None:
        super().__init__(f"route validation failed ({result.category}): {result.message}")
        self.result = result


class ConfigurationService:
    # 初始化共享 route/credential 配置事务入口
    def __init__(
        self,
        route_store: RouteStore | None = None,
        credential_store: CredentialStore | None = None,
    ) -> None:
        self.routes = route_store or RouteStore()
        self.credentials = credential_store or CredentialStore()

    # 返回不含密钥正文的配置快照
    def snapshot(self) -> ConfigurationSnapshot:
        routes = self.routes.list()
        active = self.routes.active()
        inspection = self.routes.inspect()
        credential_sources = {
            route.id: self.credentials.resolve(route.credential_ref).source for route in routes
        }
        return ConfigurationSnapshot(
            active_route_id=active.id if active is not None else None,
            routes=routes,
            credential_sources=credential_sources,
            readiness=self.readiness(active),
            route_issues=inspection.issues,
        )

    # 返回仅表示本地前置条件的脱敏 readiness，不把默认模板或未运行 doctor 伪装成可用服务
    def readiness(self, route: ProviderRoute | None = None) -> ConfigurationReadiness:
        selected = route if route is not None else self.routes.active()
        if selected is None:
            inspection = self.routes.inspect()
            if inspection.active_route_unavailable:
                return ConfigurationReadiness(
                    status="configuration_invalid",
                    local_ready=False,
                    route_id=inspection.declared_active_route_id,
                    reason="the configured active provider route is invalid or unavailable",
                )
            return ConfigurationReadiness(
                status="unconfigured",
                local_ready=False,
                reason="no active provider route is configured",
            )
        credential = self.credentials.resolve(selected.credential_ref)
        receipt = selected.receipt(credential.source)
        if credential.value is None and selected.credential_required:
            return ConfigurationReadiness(
                status="credential_missing",
                local_ready=False,
                route_id=selected.id,
                catalog_id=selected.catalog_id,
                provider=selected.provider,
                wire_format=selected.wire_format,
                model=selected.model,
                endpoint_origin=receipt.base_url_origin,
                credential_source=credential.source,
                credential_required=True,
                reason="the active route has no resolvable credential",
            )
        is_local = False
        if selected.catalog_id is not None:
            try:
                is_local = get_provider_preset(selected.catalog_id).local_probe
            except ValueError:
                is_local = False
        if selected.has_current_doctor_receipt():
            return ConfigurationReadiness(
                status="provider_verified",
                local_ready=True,
                route_id=selected.id,
                catalog_id=selected.catalog_id,
                provider=selected.provider,
                wire_format=selected.wire_format,
                model=selected.model,
                endpoint_origin=receipt.base_url_origin,
                credential_source=credential.source,
                credential_required=selected.credential_required,
                provider_validation="verified_passed",
                reason=(
                    "a bounded live Doctor receipt covers streaming, normal termination, "
                    "and every capability declared by this route/model"
                ),
            )
        if not is_local:
            stale = selected.doctor_receipt is not None
            return ConfigurationReadiness(
                status="provider_unverified",
                local_ready=False,
                route_id=selected.id,
                catalog_id=selected.catalog_id,
                provider=selected.provider,
                wire_format=selected.wire_format,
                model=selected.model,
                endpoint_origin=receipt.base_url_origin,
                credential_source=credential.source,
                credential_required=selected.credential_required,
                provider_validation="receipt_stale" if stale else "not_run",
                reason=(
                    "the saved Doctor receipt does not match this route and model"
                    if stale
                    else "the remote route has credentials but has not passed a basic Doctor probe"
                ),
            )
        return ConfigurationReadiness(
            status="configuration_complete",
            local_ready=True,
            route_id=selected.id,
            catalog_id=selected.catalog_id,
            provider=selected.provider,
            wire_format=selected.wire_format,
            model=selected.model,
            endpoint_origin=receipt.base_url_origin,
            credential_source=credential.source,
            credential_required=selected.credential_required,
            reason=(
                "local route prerequisites are present; provider validation has not run"
                if selected.credential_required
                else "this loopback route requires no credential; endpoint probe has not run"
            ),
        )

    # 对 catalog 标记的本地 Provider 执行可注入端口探测并合并脱敏 readiness
    async def probe_readiness(
        self,
        route: ProviderRoute | None = None,
        *,
        connector: LocalPortConnector | None = None,
        doctor: ProviderDoctor | None = None,
    ) -> ConfigurationReadiness:
        selected = route if route is not None else self.routes.active()
        readiness = self.readiness(selected)
        if selected is None:
            return readiness
        preset = None
        if selected.catalog_id is not None:
            try:
                preset = get_provider_preset(selected.catalog_id)
            except ValueError:
                preset = None
        if preset is not None and preset.local_probe:
            local_result = await probe_local_provider(
                preset,
                endpoint=str(selected.base_url),
                connector=connector,
            )
            if local_result.reachable:
                return readiness.model_copy(
                    update={
                        "provider_validation": "endpoint_reachable",
                        "reason": local_result.reason,
                    }
                )
            return readiness.model_copy(
                update={
                    "status": "endpoint_unreachable",
                    "local_ready": False,
                    "provider_validation": "endpoint_unreachable",
                    "reason": local_result.reason,
                }
            )
        if readiness.local_ready:
            return readiness
        doctor_result = await self.validate_route(selected, doctor=doctor)
        if doctor_result.status != "ok":
            return readiness.model_copy(
                update={
                    "provider_validation": "basic_failed",
                    "reason": f"required Doctor probe failed ({doctor_result.category})",
                }
            )
        validated = selected.model_copy(
            update={"doctor_receipt": doctor_result.to_receipt(selected)}
        )
        self.routes.update(validated)
        return self.readiness(validated)

    # 新增或更新 route，并在带密钥提交失败时尽力恢复原凭据
    def save_route(
        self,
        route: ProviderRoute,
        *,
        secret: str | None = None,
        activate: bool = False,
        update: bool = False,
    ) -> ProviderRoute:
        previous_route = self.routes.get(route.id) if update else None
        previous_secret = (
            self.credentials.resolve(previous_route.credential_ref).value
            if previous_route is not None
            else None
        )
        new_ref: str | None = None
        if secret is not None:
            new_ref = self.credentials.save(route.id, secret)
            route = ProviderRoute.model_validate(
                {**route.model_dump(mode="python"), "credential_ref": new_ref}
            )
        try:
            self.routes.commit(route, update=update, activate=activate)
        except Exception:
            if new_ref is not None:
                self._restore_credential(route.id, new_ref, previous_secret)
            raise
        return route

    # 在不修改存储的前提下诊断候选 route，显式密钥仅在本次请求内存中使用
    async def validate_route(
        self,
        route: ProviderRoute,
        *,
        secret: str | None = None,
        doctor: ProviderDoctor | None = None,
    ) -> ProviderDoctorResult:
        credential = (
            CredentialResolution(value=secret, source="keyring")
            if secret is not None
            else self.credentials.resolve(route.credential_ref)
        )
        return await (doctor or ProviderDoctor()).check(route, credential)

    # 先完成真实 provider 诊断，成功后才原子提交 route、活动项和可选凭据
    async def save_route_checked(
        self,
        route: ProviderRoute,
        *,
        secret: str | None = None,
        activate: bool = False,
        update: bool = False,
        doctor: ProviderDoctor | None = None,
    ) -> ProviderRoute:
        result = await self.validate_route(route, secret=secret, doctor=doctor)
        if result.status != "ok":
            raise ConfigurationValidationError(result)
        try:
            route = route.model_copy(update={"doctor_receipt": result.to_receipt(route)})
        except ValueError as exc:
            raise ConfigurationValidationError(result) from exc
        return self.save_route(
            route,
            secret=secret,
            activate=activate,
            update=update,
        )

    # 将指定 route 设为活动项
    def set_active(self, route_id: str) -> ProviderRoute:
        return self.routes.set_active(route_id)

    # 先对目标 route/model 执行 Doctor，再切换活动项并持久化收据
    async def set_active_checked(
        self,
        route_id: str,
        *,
        doctor: ProviderDoctor | None = None,
    ) -> ProviderRoute:
        route = self.routes.get(route_id)
        result = await self.validate_route(route, doctor=doctor)
        if result.status != "ok":
            raise ConfigurationValidationError(result)
        try:
            validated = route.model_copy(
                update={"doctor_receipt": result.to_receipt(route)}
            )
        except ValueError as exc:
            raise ConfigurationValidationError(result) from exc
        self.routes.commit(validated, update=True, activate=True)
        return self.routes.get(route_id)

    # 删除 route，并按显式开关处理其凭据
    def remove_route(self, route_id: str, *, delete_credential: bool = False) -> None:
        route = self.routes.get(route_id)
        self.routes.remove(route_id)
        if delete_credential:
            self.credentials.delete(route.credential_ref)

    # 在 route 提交失败时恢复旧密钥或删除本次新建引用
    def _restore_credential(
        self,
        route_id: str,
        new_ref: str,
        previous_secret: str | None,
    ) -> None:
        try:
            if previous_secret is None:
                self.credentials.delete(new_ref)
            else:
                self.credentials.save(
                    route_id,
                    previous_secret,
                    prefer_keyring=new_ref.startswith("keyring:"),
                )
        except Exception as exc:
            raise RouteStoreError(
                "route commit failed and credential rollback also failed"
            ) from exc

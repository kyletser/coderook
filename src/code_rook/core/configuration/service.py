from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from code_rook.core.llm.credentials import CredentialResolution, CredentialStore
from code_rook.core.llm.doctor import ProviderDoctor, ProviderDoctorResult
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.llm.routes import ProviderRoute


class ConfigurationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_route_id: str | None
    routes: tuple[ProviderRoute, ...]
    credential_sources: dict[str, str]


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
        return ConfigurationSnapshot(
            active_route_id=active.id if active is not None else None,
            routes=routes,
            credential_sources={
                route.id: self.credentials.resolve(route.credential_ref).source
                for route in routes
            },
        )

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
        return self.save_route(
            route,
            secret=secret,
            activate=activate,
            update=update,
        )

    # 将指定 route 设为活动项
    def set_active(self, route_id: str) -> ProviderRoute:
        return self.routes.set_active(route_id)

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

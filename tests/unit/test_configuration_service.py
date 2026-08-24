from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.configuration import ConfigurationService
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctorCheck, ProviderDoctorResult
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.llm.routes import ProviderRoute, get_route_preset


class _NoKeyring:
    # 模拟不可用的系统 keyring
    def get_password(self, service: str, account: str) -> str | None:
        del service, account
        return None

    # 模拟 keyring 写入失败以强制使用权限收紧文件
    def set_password(self, service: str, account: str, password: str) -> None:
        del service, account, password
        raise RuntimeError("keyring unavailable")

    # 模拟不存在的 keyring 删除
    def delete_password(self, service: str, account: str) -> None:
        del service, account


class _Doctor:
    def __init__(self, status: str) -> None:
        self.status = status

    # 返回固定脱敏诊断结论，验证事务发生在 doctor 成功之后
    async def check(self, route: ProviderRoute, credential: object) -> ProviderDoctorResult:
        del credential
        return ProviderDoctorResult(
            status="ok" if self.status == "ok" else "error",
            category="ok" if self.status == "ok" else "network",
            route_id=route.id,
            message="route is ready" if self.status == "ok" else "unreachable",
            credential_source="keyring",
            readiness="verified" if self.status == "ok" else "failed",
            route_digest=route.validation_digest(),
            checked_at="2026-08-24T00:00:00+00:00",
            basic=ProviderDoctorCheck(
                status="passed" if self.status == "ok" else "failed",
                message="route is ready" if self.status == "ok" else "unreachable",
            ),
            capabilities={
                "streaming": ProviderDoctorCheck(
                    status="passed" if self.status == "ok" else "failed",
                    message="streaming probe",
                ),
                "termination": ProviderDoctorCheck(
                    status="passed" if self.status == "ok" else "failed",
                    message="termination probe",
                ),
                "tool_calling": ProviderDoctorCheck(
                    status="passed" if self.status == "ok" else "not_run",
                    message="tool probe",
                ),
                "parallel_tools": ProviderDoctorCheck(
                    status="passed" if self.status == "ok" else "not_run",
                    message="parallel probe",
                ),
                "images": ProviderDoctorCheck(
                    status="unsupported",
                    message="images unsupported",
                ),
            },
        )


# 构造用于配置事务测试的合法 HTTPS route
def _route(route_id: str = "demo") -> ProviderRoute:
    return ProviderRoute(
        id=route_id,
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url="https://example.com/v1/chat/completions",
        model="demo-model",
        credential_ref=f"file:{route_id}",
    )


# 功能：验证 ConfigurationService 原子入口同时提交 route、活动项和凭据来源
# 设计：禁用 keyring 强制写隔离 credentials 文件，再读取无密钥快照核对三项投影
def test_configuration_service_saves_route_and_secret(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    service = ConfigurationService(routes, credentials)

    saved = service.save_route(_route(), secret="secret", activate=True)
    snapshot = service.snapshot()

    assert saved.credential_ref == "file:demo"
    assert snapshot.active_route_id == "demo"
    assert snapshot.credential_sources == {"demo": "file"}
    assert snapshot.readiness.status == "provider_unverified"
    assert snapshot.readiness.local_ready is False
    assert snapshot.readiness.provider_validation == "not_run"
    assert snapshot.readiness.endpoint_origin == "https://example.com"
    assert "secret" not in snapshot.model_dump_json()


# 功能：验证 fresh install 不会把内置默认模板伪装成已配置活动 route
# 设计：使用完全空的 route 和 credential 存储读取快照，断言 readiness 不包含默认 provider 或模型
def test_configuration_snapshot_reports_fresh_install_as_unconfigured(
    tmp_path: Path,
) -> None:
    service = ConfigurationService(
        RouteStore(tmp_path / "routes.json"),
        CredentialStore(tmp_path / "credentials.json", backend=_NoKeyring()),
    )

    snapshot = service.snapshot()

    assert snapshot.active_route_id is None
    assert snapshot.routes == ()
    assert snapshot.readiness.status == "unconfigured"
    assert snapshot.readiness.local_ready is False
    assert snapshot.readiness.route_id is None
    serialized = snapshot.readiness.model_dump_json()
    assert "legacy-anthropic" not in serialized
    assert "claude-sonnet" not in serialized


# 功能：验证活动 route 缺少凭据时 readiness 明确失败且只暴露 endpoint origin
# 设计：保存含敏感 credential 引用名和 URL 路径的 route 但不保存密钥，检查脱敏本地状态投影
def test_configuration_readiness_reports_missing_credential_without_reference(
    tmp_path: Path,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    service = ConfigurationService(routes, credentials)
    route = _route().model_copy(update={"credential_ref": "file:sensitive-account-name"})
    service.save_route(route, activate=True)

    readiness = service.readiness()

    assert readiness.status == "credential_missing"
    assert readiness.local_ready is False
    assert readiness.credential_source == "missing"
    assert readiness.endpoint_origin == "https://example.com"
    serialized = readiness.model_dump_json()
    assert "/v1/chat/completions" not in serialized
    assert "sensitive-account-name" not in serialized


# 功能：验证 Ollama 活动 route 在无任何凭据时仍通过本地配置前置检查
# 设计：从统一 catalog preset 建立 route 并使用空凭据存储，区分免密设计与远端 key 缺失
def test_configuration_readiness_accepts_catalog_local_route_without_key(
    tmp_path: Path,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    route = get_route_preset("ollama").model_copy(update={"id": "local"})
    routes.add(route, activate=True)

    readiness = ConfigurationService(routes, credentials).readiness()

    assert readiness.status == "configuration_complete"
    assert readiness.local_ready is True
    assert readiness.credential_required is False
    assert readiness.catalog_id == "ollama"
    assert readiness.credential_source == "missing"
    assert readiness.provider_validation == "not_run"


# 功能：验证本地 Provider 端口探测结果进入统一 readiness 且不会发起真实网络请求
# 设计：先后注入不可达与可达连接器，检查同一 route 的状态和 validation 投影准确翻转
async def test_configuration_probe_readiness_uses_injected_local_connector(
    tmp_path: Path,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    routes.add(get_route_preset("lm-studio"), activate=True)
    service = ConfigurationService(routes, credentials)
    calls: list[tuple[str, int]] = []

    # 模拟不可达端口并记录 catalog 的 LM Studio 默认地址
    async def unreachable(host: str, port: int, timeout_s: float) -> bool:
        del timeout_s
        calls.append((host, port))
        return False

    # 模拟同一端口随后变为可达
    async def reachable(host: str, port: int, timeout_s: float) -> bool:
        del host, port, timeout_s
        return True

    failed = await service.probe_readiness(connector=unreachable)
    ready = await service.probe_readiness(connector=reachable)

    assert calls == [("127.0.0.1", 1234)]
    assert failed.status == "endpoint_unreachable"
    assert failed.local_ready is False
    assert failed.provider_validation == "endpoint_unreachable"
    assert ready.status == "configuration_complete"
    assert ready.provider_validation == "endpoint_reachable"


# 功能：验证 route 提交失败时本次新凭据会被回滚而不留下孤儿密钥
# 设计：先建立同名 route，再以 add 模式触发 duplicate 错误，随后确认 file 引用仍缺失
def test_configuration_service_rolls_back_secret_on_route_failure(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    routes.add(_route())
    service = ConfigurationService(routes, credentials)

    with pytest.raises(RouteStoreError, match="already exists"):
        service.save_route(_route(), secret="orphan", activate=True)

    assert credentials.resolve("file:demo").source == "missing"


# 功能：验证 route 更新与活动项切换只执行一次原子文件替换
# 设计：监视 RouteStore._save 调用次数，更新非活动 route 并激活后核对文档不会出现中间状态
def test_configuration_service_updates_and_activates_in_one_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    routes.add(_route("first"), activate=True)
    routes.add(_route("second"))
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    service = ConfigurationService(routes, credentials)
    saves = 0
    original = routes._save

    # 统计原子文档替换次数，确保 update 与 activate 没有拆成两次提交
    def counted(document: object) -> None:
        nonlocal saves
        saves += 1
        original(document)  # type: ignore[arg-type]

    monkeypatch.setattr(routes, "_save", counted)
    service.save_route(
        _route("second").model_copy(update={"model": "updated"}),
        update=True,
        activate=True,
    )

    assert saves == 1
    assert routes.active() is not None and routes.active().id == "second"
    assert routes.get("second").model == "updated"


# 功能：验证候选 route 的 doctor 失败不会写入 route、活动项或凭据
# 设计：注入固定失败诊断器并传入内存密钥，断言三个持久存储投影都保持为空
async def test_configuration_service_doctors_before_commit(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    service = ConfigurationService(routes, credentials)

    with pytest.raises(RuntimeError, match="route validation failed"):
        await service.save_route_checked(
            _route(),
            secret="never-persist",
            activate=True,
            doctor=_Doctor("error"),  # type: ignore[arg-type]
        )

    assert routes.list() == ()
    assert routes.active() is None
    assert credentials.resolve("file:demo").source == "missing"


# 功能：验证 Doctor 即使顶层误报 ok，必需分项 failed 或 not_run 仍不能提交 route
# 设计：注入缺少正常终止证据的摘要绑定结果，锁定收据层的第二道 fail-closed 门禁
async def test_configuration_service_rejects_incomplete_doctor_receipt(
    tmp_path: Path,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    service = ConfigurationService(routes, credentials)

    class _IncompleteDoctor:
        # 返回顶层成功但终止分项失败的伪诊断结果
        async def check(
            self,
            route: ProviderRoute,
            credential: object,
        ) -> ProviderDoctorResult:
            del credential
            return ProviderDoctorResult(
                status="ok",
                category="ok",
                route_id=route.id,
                message="incorrect top-level success",
                credential_source="keyring",
                readiness="verified",
                route_digest=route.validation_digest(),
                checked_at="2026-08-24T00:00:00+00:00",
                basic=ProviderDoctorCheck(status="passed", message="basic passed"),
                capabilities={
                    "streaming": ProviderDoctorCheck(
                        status="passed", message="stream passed"
                    ),
                    "termination": ProviderDoctorCheck(
                        status="failed", message="terminal missing"
                    ),
                },
            )

    with pytest.raises(RuntimeError, match="route validation failed"):
        await service.save_route_checked(
            _route(),
            secret="never-persist",
            activate=True,
            doctor=_IncompleteDoctor(),  # type: ignore[arg-type]
        )

    assert routes.list() == ()
    assert credentials.resolve("file:demo").source == "missing"


# 功能：验证成功 Doctor 收据持久绑定 route/model，模型改变后 readiness 立即失效
# 设计：先走 checked 保存得到 verified，再仅复制旧收据修改 model，断言摘要不匹配被识别为 stale
async def test_remote_readiness_requires_current_route_model_receipt(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_NoKeyring(),
    )
    service = ConfigurationService(routes, credentials)

    saved = await service.save_route_checked(
        _route(),
        secret="verified-secret",
        activate=True,
        doctor=_Doctor("ok"),  # type: ignore[arg-type]
    )

    assert saved.has_current_doctor_receipt()
    assert service.readiness().status == "provider_verified"
    assert service.readiness().local_ready is True

    changed = saved.model_copy(update={"model": "different-model"})
    routes.update(changed)
    stale = service.readiness()

    assert stale.status == "provider_unverified"
    assert stale.provider_validation == "receipt_stale"
    assert stale.local_ready is False


# 功能：验证远程 route 在任务前完整探测成功后持久化摘要绑定 Doctor 收据
# 设计：先用未受检 save 形成 blocked readiness，再注入完整 Doctor 并从磁盘重建服务核对证据
async def test_remote_probe_persists_verified_doctor_receipt(tmp_path: Path) -> None:
    route_path = tmp_path / "routes.json"
    credential_path = tmp_path / "credentials.json"
    routes = RouteStore(route_path)
    credentials = CredentialStore(credential_path, backend=_NoKeyring())
    service = ConfigurationService(routes, credentials)
    service.save_route(_route(), secret="verified-secret", activate=True)

    readiness = await service.probe_readiness(
        doctor=_Doctor("ok"),  # type: ignore[arg-type]
    )
    reloaded = ConfigurationService(
        RouteStore(route_path),
        CredentialStore(credential_path, backend=_NoKeyring()),
    ).readiness()

    assert readiness.status == "provider_verified"
    assert readiness.provider_validation == "verified_passed"
    assert reloaded.local_ready is True


# 功能：验证活动 route 切换的 Doctor 失败时旧活动项和目标 route 都不发生修改
# 设计：先建立双 route，再注入网络失败 Doctor 并核对原子文档仍指向旧活动项且无新收据
async def test_set_active_checked_keeps_previous_route_on_failure(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    routes.add(_route("first"), activate=True)
    routes.add(_route("second"))
    service = ConfigurationService(
        routes,
        CredentialStore(tmp_path / "credentials.json", backend=_NoKeyring()),
    )

    with pytest.raises(RuntimeError, match="route validation failed"):
        await service.set_active_checked(
            "second",
            doctor=_Doctor("error"),  # type: ignore[arg-type]
        )

    assert routes.active() is not None and routes.active().id == "first"
    assert routes.get("second").doctor_receipt is None

from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.configuration import ConfigurationService
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctorResult
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.llm.routes import ProviderRoute


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
    assert "secret" not in snapshot.model_dump_json()


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

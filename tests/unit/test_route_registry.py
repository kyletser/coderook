from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from code_rook.core.config import LlmConfig, get_config
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.migration_receipt import (
    ProviderCatalogMigrationReceiptError,
    ProviderCatalogMigrationReceiptStore,
)
from code_rook.core.llm.route_registry import (
    RouteRegistry,
    RouteResolutionError,
    legacy_config_route,
)
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import get_route_preset


class _UnavailableBackend:
    # 始终模拟不可用 keyring，使测试稳定使用临时凭据文件
    def get_password(self, service: str, account: str) -> str | None:
        return None

    # 模拟 keyring 写入失败
    def set_password(self, service: str, account: str, password: str) -> None:
        raise RuntimeError("unavailable")

    # 模拟 keyring 删除失败
    def delete_password(self, service: str, account: str) -> None:
        raise RuntimeError("unavailable")


# 构造隔离的 route registry 与两个底层 store
def _registry(tmp_path: Path) -> tuple[RouteRegistry, RouteStore, CredentialStore]:
    routes = RouteStore(tmp_path / "routes.json")
    credentials = CredentialStore(
        tmp_path / "credentials.json",
        backend=_UnavailableBackend(),
    )
    registry = RouteRegistry(
        LlmConfig(),
        route_store=routes,
        credential_store=credentials,
    )
    return registry, routes, credentials


# 功能：验证活动 route 解析得到真实凭据与敏感信息隔离的 receipt
# 设计：文件凭据与 route 分开保存，resolve 后检查 secret 只在 repr 隐藏字段中使用
def test_registry_resolves_active_route_and_receipt(tmp_path: Path) -> None:
    registry, routes, credentials = _registry(tmp_path)
    reference = credentials.save("zen", "zen-secret")
    route = get_route_preset("opencode-zen").model_copy(
        update={"id": "zen", "credential_ref": reference}
    )
    routes.add(route, activate=True)

    resolved = registry.resolve()

    assert resolved.credential == "zen-secret"
    assert resolved.receipt.route_id == "zen"
    assert resolved.receipt.credential_source == "file"
    assert "zen-secret" not in repr(resolved)
    assert "zen-secret" not in resolved.receipt.model_dump_json()


# 功能：验证 benchmark 温度覆盖应用到配置 route 和 receipt，但不修改持久 route 文件
# 设计：对同一临时 store 创建带 override 的第二 registry，比较解析值与原始存储值
def test_registry_applies_ephemeral_temperature_override(tmp_path: Path) -> None:
    _registry_default, routes, credentials = _registry(tmp_path)
    reference = credentials.save("deterministic", "secret")
    route = get_route_preset("openai").model_copy(
        update={"id": "deterministic", "credential_ref": reference}
    )
    routes.add(route, activate=True)
    overridden = RouteRegistry(
        LlmConfig(),
        route_store=routes,
        credential_store=credentials,
        temperature_override=0.0,
    )

    resolved = overridden.resolve()

    assert resolved.route.temperature == 0.0
    assert resolved.receipt.temperature == 0.0
    assert routes.get("deterministic").temperature is None


# 功能：验证切换 route 只改变选择，不覆盖任何另一 route 凭据
# 设计：保存两条独立 file ref，来回切换后分别解析并比较原始值
def test_registry_switches_routes_without_overwriting_credentials(tmp_path: Path) -> None:
    registry, routes, credentials = _registry(tmp_path)
    first_ref = credentials.save("first", "first-secret")
    second_ref = credentials.save("second", "second-secret")
    routes.add(
        get_route_preset("openai").model_copy(update={"id": "first", "credential_ref": first_ref})
    )
    routes.add(
        get_route_preset("anthropic").model_copy(
            update={"id": "second", "credential_ref": second_ref}
        )
    )

    routes.set_active("first")
    first = registry.resolve()
    routes.set_active("second")
    second = registry.resolve()

    assert first.credential == "first-secret"
    assert second.credential == "second-secret"
    assert credentials.resolve(first_ref).value == "first-secret"


# 功能：验证缺失 route 凭据给出不含 ref 或 secret 的稳定错误
# 设计：route 指向不存在的 file ref，扫描异常只允许出现 route ID
def test_registry_missing_credential_error_is_redacted(tmp_path: Path) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    route = get_route_preset("openai").model_copy(
        update={"id": "safe-id", "credential_ref": "file:sensitive-reference"}
    )
    routes.add(route)

    with pytest.raises(RouteResolutionError) as captured:
        registry.resolve()

    message = str(captured.value)
    assert "safe-id" in message
    assert "sensitive-reference" not in message


# 功能：验证 catalog 声明的 Ollama 免密 route 可被运行时解析为显式空凭据
# 设计：使用空 CredentialStore 解析本地 preset，断言不会伪造 key 且 receipt 保持 missing 来源
def test_registry_resolves_credential_free_local_route(tmp_path: Path) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    routes.add(get_route_preset("ollama"), activate=True)

    resolved = registry.resolve()

    assert resolved.credential == ""
    assert resolved.route.credential_required is False
    assert resolved.receipt.credential_source == "missing"


# 功能：后台 Worker 路由不能把仅有凭据但未通过 Doctor 的远端配置当成 ready
# 设计：保存有真实 file credential 但无 receipt 的远端 route，断言 resolve_ready 在网络调用前失败关闭
@pytest.mark.asyncio
async def test_resolve_ready_rejects_unverified_remote_route(tmp_path: Path) -> None:
    registry, routes, credentials = _registry(tmp_path)
    reference = credentials.save("remote", "secret")
    route = get_route_preset("openai").model_copy(
        update={"id": "remote", "credential_ref": reference}
    )
    routes.add(route, activate=True)

    with pytest.raises(RouteResolutionError, match="provider_unverified"):
        await registry.resolve_ready()


# 功能：Worker retry 的原 route 摘要变化必须在 endpoint 探测前被拒绝
# 设计：使用本地免密 route 和错误摘要，确保 fail-closed 不依赖本机 Ollama 是否运行
@pytest.mark.asyncio
async def test_resolve_ready_rejects_stale_worker_route_digest(tmp_path: Path) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    route = get_route_preset("ollama")
    routes.add(route, activate=True)

    with pytest.raises(RouteResolutionError, match="changed since worker creation"):
        await registry.resolve_ready(expected_digest="0" * 64)


# 功能：验证未迁移用户仍通过显式旧配置映射使用原 Provider
# 设计：构造 DeepSeek 配置并检查 provider/wire/endpoint，而模型名称使用无前缀值
def test_legacy_config_route_is_explicit_and_model_agnostic() -> None:
    route = legacy_config_route(
        LlmConfig(
            provider="deepseek",
            default_model="custom-no-prefix",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
        )
    )

    assert route.provider == "openai-compatible"
    assert route.wire_format == "openai_chat"
    assert route.model == "custom-no-prefix"


# 功能：验证默认 RouteRegistry 能直接解析显式 env 文件携带的旧配置凭据
# 设计：不注入 CredentialStore 且不修改进程环境，覆盖 CoreApp 实际使用的默认构造路径
def test_registry_default_store_consumes_explicit_env_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_DEPLOY_KEY", raising=False)
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        "CODEROOK_LLM_PROVIDER=deepseek\n"
        "CODEROOK_LLM_DEFAULT_MODEL=deepseek-v4-pro\n"
        "CODEROOK_LLM_BASE_URL=https://api.deepseek.com/chat/completions\n"
        "CODEROOK_LLM_API_KEY_ENV=DEEPSEEK_DEPLOY_KEY\n"
        "DEEPSEEK_DEPLOY_KEY=explicit-file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = get_config(env_file=env_file, environ={})
    registry = RouteRegistry(
        config.llm,
        route_store=RouteStore(tmp_path / "routes.json"),
    )

    resolved = registry.resolve()

    assert resolved.credential == "explicit-file-secret"
    assert resolved.route.credential_ref == "env:DEEPSEEK_DEPLOY_KEY"
    assert resolved.receipt.credential_source == "env"
    assert "explicit-file-secret" not in repr(resolved)


# 功能：验证旧 LlmConfig 只在空 RouteStore 中一次性迁移为活动 catalog route
# 设计：连续调用迁移入口，断言第一次持久化、第二次幂等且不覆盖用户 route
def test_registry_migrates_legacy_config_once(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    registry = RouteRegistry(
        LlmConfig(
            provider="deepseek",
            default_model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        route_store=routes,
        credential_store=CredentialStore(
            tmp_path / "credentials.json",
            backend=_UnavailableBackend(),
        ),
    )

    migrated = registry.migrate_legacy_config()
    repeated = registry.migrate_legacy_config()

    assert migrated is not None
    assert migrated.id == "legacy-deepseek"
    assert repeated is None
    assert routes.active() == migrated
    receipt = ProviderCatalogMigrationReceiptStore(tmp_path).load()
    assert receipt.outcome == "migrated"


# 功能：验证旧配置迁移与并发用户 Route 写入不会产生读改写覆盖
# 设计：迁移读取空 Catalog 后暂停并启动另一 Store 写入，断言事务串行化且两条 Route 均落盘
def test_registry_migration_preserves_concurrent_user_route(tmp_path: Path) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    loaded = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []
    original_load = routes._load

    # 在迁移事务已经读出空 Catalog 且仍持锁时制造并发写入窗口
    def paused_first_load():
        document = original_load()
        if not loaded.is_set():
            loaded.set()
            if not release.wait(timeout=5):
                raise TimeoutError("concurrent migration test did not resume")
        return document

    routes._load = paused_first_load  # type: ignore[method-assign]

    # 在线程中运行旧配置迁移并捕获所有失败供主线程断言
    def migrate() -> None:
        try:
            registry.migrate_legacy_config()
        except BaseException as exc:
            errors.append(exc)

    # 在线程中模拟用户同时保存独立的本地 Provider Route
    def add_user_route() -> None:
        try:
            RouteStore(routes.path).add(
                get_route_preset("ollama").model_copy(update={"id": "user-local"})
            )
        except BaseException as exc:
            errors.append(exc)

    migration_thread = threading.Thread(target=migrate)
    user_thread = threading.Thread(target=add_user_route)
    migration_thread.start()
    assert loaded.wait(timeout=5)
    user_thread.start()
    release.set()
    migration_thread.join(timeout=5)
    user_thread.join(timeout=5)

    assert errors == []
    assert not migration_thread.is_alive()
    assert not user_thread.is_alive()
    assert {route.id for route in RouteStore(routes.path).list()} == {
        "legacy-anthropic",
        "user-local",
    }
    assert ProviderCatalogMigrationReceiptStore(tmp_path).load().outcome == "migrated"


# 功能：验证无旧配置时 Registry 不创建虚假 route 但仍记录无需迁移的完成收据
# 设计：显式传入 legacy_configured=False，检查空 catalog、返回值和收据 outcome 三者一致
def test_registry_receipts_confirmed_no_legacy_migration(tmp_path: Path) -> None:
    registry, routes, _credentials = _registry(tmp_path)

    migrated = registry.migrate_legacy_config(legacy_configured=False)

    assert migrated is None
    assert routes.list() == ()
    receipt = ProviderCatalogMigrationReceiptStore(tmp_path).load()
    assert receipt.outcome == "legacy_not_configured"


# 功能：验证已有 Route Catalog 但缺失旧迁移证据时会补写 catalog_present 收据
# 设计：先直接保存本地 route 再执行迁移入口，断言 route 不变且完成收据绑定现有输出
def test_registry_receipts_preexisting_catalog_without_remigration(
    tmp_path: Path,
) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    route = get_route_preset("ollama")
    routes.add(route, activate=True)

    migrated = registry.migrate_legacy_config(legacy_configured=False)

    assert migrated is None
    assert routes.active() == route
    receipt = ProviderCatalogMigrationReceiptStore(tmp_path).load()
    assert receipt.outcome == "catalog_present"


# 功能：验证损坏迁移收据会在写 Route Catalog 前失败关闭
# 设计：预置非法 receipt 并调用真实迁移入口，断言异常后 routes.json 仍不存在
def test_registry_invalid_migration_receipt_blocks_catalog_mutation(
    tmp_path: Path,
) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    receipt = ProviderCatalogMigrationReceiptStore(tmp_path)
    receipt.path.parent.mkdir(parents=True)
    receipt.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(RouteResolutionError, match="receipt is invalid"):
        registry.migrate_legacy_config()

    assert routes.list() == ()
    assert not routes.path.exists()
    assert receipt.path.read_text(encoding="utf-8") == "not-json"


# 功能：验证完成收据配空 Catalog 时不会重迁移或在下一次启动静默恢复健康
# 设计：先完成 Anthropic 迁移再清空 Catalog 并切换旧配置，连续两次调用均应失败且文件字节不变
def test_registry_completed_receipt_with_empty_catalog_stays_failed_closed(
    tmp_path: Path,
) -> None:
    registry, routes, credentials = _registry(tmp_path)
    registry.migrate_legacy_config()
    receipt_store = ProviderCatalogMigrationReceiptStore(tmp_path)
    receipt_before = receipt_store.path.read_bytes()
    for route in routes.list():
        routes.remove(route.id)
    catalog_before = routes.path.read_bytes()
    changed = RouteRegistry(
        LlmConfig(
            provider="deepseek",
            default_model="deepseek-chat",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
        ),
        route_store=routes,
        credential_store=credentials,
    )

    for _attempt in range(2):
        with pytest.raises(RouteResolutionError, match="complete.*Catalog is empty"):
            changed.migrate_legacy_config()
        assert routes.list() == ()
        assert routes.path.read_bytes() == catalog_before
        assert receipt_store.path.read_bytes() == receipt_before


# 功能：验证首次旧配置迁移的收据写入失败会精确恢复原 Route Catalog
# 设计：分别从缺失文件和自定义格式空文档开始注入收据故障，断言存在性与原始字节均不变化
def test_registry_receipt_failure_rolls_back_exact_route_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formatted_empty = (
        b'{\n  "version": 1,\n  "active_route_id": null,\n  "routes": []\n}\n'
    )

    for case, original in (("missing", None), ("existing", formatted_empty)):
        state = tmp_path / case
        state.mkdir()
        routes = RouteStore(state / "routes.json")
        if original is not None:
            routes.path.write_bytes(original)
        receipts = ProviderCatalogMigrationReceiptStore(state)
        registry = RouteRegistry(
            LlmConfig(),
            route_store=routes,
            credential_store=CredentialStore(
                state / "credentials.json",
                backend=_UnavailableBackend(),
            ),
            migration_receipt_store=receipts,
        )

        # 模拟收据在 Route 已提交后仍无法持久化
        def fail_write(_receipt: object) -> None:
            raise ProviderCatalogMigrationReceiptError("injected receipt write failure")

        monkeypatch.setattr(receipts, "write", fail_write)

        with pytest.raises(
            ProviderCatalogMigrationReceiptError,
            match="injected receipt write failure",
        ):
            registry.migrate_legacy_config()

        assert routes.path.exists() is (original is not None)
        if original is not None:
            assert routes.path.read_bytes() == original
        assert receipts.inspect() == "pending"


# 功能：验证坏 active route 不会静默回退到旧 LlmConfig 并误用另一 Provider
# 设计：文档同时保留一条好 route、一个坏 active，调用默认解析入口应明确 unavailable
def test_registry_rejects_invalid_active_route_instead_of_legacy_fallback(
    tmp_path: Path,
) -> None:
    registry, routes, _credentials = _registry(tmp_path)
    good = get_route_preset("ollama").model_copy(update={"id": "good"})
    bad = {
        **good.model_dump(mode="json"),
        "id": "bad",
        "supports_tools": False,
        "supports_parallel_tools": True,
    }
    routes.path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_route_id": "bad",
                "routes": [good.model_dump(mode="json"), bad],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RouteResolutionError, match="invalid or unavailable"):
        registry.route()

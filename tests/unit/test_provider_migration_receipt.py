from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.config import LlmConfig
from code_rook.core.llm.migration_receipt import (
    ProviderCatalogMigrationReceiptError,
    ProviderCatalogMigrationReceiptStore,
    build_provider_catalog_migration_receipt,
    inspect_provider_catalog_migration,
)
from code_rook.core.llm.route_store import RouteStore


# 构造不含真实密钥的测试配置并允许凭据覆盖正文参与泄漏检查
def _config() -> LlmConfig:
    return LlmConfig(
        provider="deepseek",
        default_model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/chat/completions",
        api_key_env="DEEPSEEK_TEST_KEY",
        credential_overlay={"DEEPSEEK_TEST_KEY": "never-write-this-secret"},
    )


# 功能：验证缺失、完整、篡改三种 Provider Catalog 迁移收据状态
# 设计：对同一路径依次检查、原子写入和修改摘要，证明 inspect 只读且完整性校验失败关闭
def test_provider_migration_receipt_has_strict_read_only_states(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    store = ProviderCatalogMigrationReceiptStore(tmp_path)

    assert store.inspect() == "pending"
    receipt = build_provider_catalog_migration_receipt(
        _config(),
        routes,
        outcome="legacy_not_configured",
    )
    store.write(receipt)
    before = store.path.read_bytes()

    assert store.inspect() == "complete"
    assert inspect_provider_catalog_migration(tmp_path) == "complete"
    assert store.path.read_bytes() == before

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["route_catalog_digest"] = "f" * 64
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.inspect() == "invalid"
    assert json.loads(store.path.read_text(encoding="utf-8")) == payload


# 功能：验证未来 schema 收据被判为 invalid 且检查过程保留原证据
# 设计：直接写入结构完整但版本为二的文档，排除 Pydantic 默认值把未来版本降级的风险
def test_provider_migration_receipt_rejects_future_schema(tmp_path: Path) -> None:
    store = ProviderCatalogMigrationReceiptStore(tmp_path)
    store.path.parent.mkdir(parents=True)
    payload = {
        "schema_version": 2,
        "migration_id": "provider-catalog-v1",
        "completed_at": "2026-08-24T00:00:00Z",
        "outcome": "catalog_present",
        "legacy_config_digest": "a" * 64,
        "route_catalog_digest": "b" * 64,
        "receipt_digest": "c" * 64,
    }
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.inspect() == "invalid"
    assert json.loads(store.path.read_text(encoding="utf-8")) == payload


# 功能：验证迁移目录是符号链接时缺失收据也按 invalid 失败关闭
# 设计：让 migrations 指向状态根外目录并检查目标无写入，防止 pending 掩盖路径劫持
def test_provider_migration_receipt_rejects_symlinked_directory(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    try:
        (state / "migrations").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    store = ProviderCatalogMigrationReceiptStore(state)

    assert store.inspect() == "invalid"
    assert not list(outside.iterdir())


# 功能：验证迁移收据只保存摘要且不泄漏显式环境文件中的密钥正文
# 设计：让 secret 进入 credential_overlay，扫描落盘 JSON 并校验仅存在固定长度摘要
def test_provider_migration_receipt_never_persists_secret_values(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    store = ProviderCatalogMigrationReceiptStore(tmp_path)
    receipt = build_provider_catalog_migration_receipt(
        _config(),
        routes,
        outcome="legacy_not_configured",
    )

    store.write(receipt)

    raw = store.path.read_text(encoding="utf-8")
    assert "never-write-this-secret" not in raw
    assert "DEEPSEEK_TEST_KEY" not in raw
    assert len(receipt.legacy_config_digest) == 64
    assert len(receipt.route_catalog_digest) == 64


# 功能：验证不同的第二份有效迁移收据不能覆盖已经落盘的历史证据
# 设计：首份收据写入后改变 Route Catalog 摘要，断言冲突失败且文件字节保持不变
def test_provider_migration_receipt_refuses_conflicting_rewrite(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    store = ProviderCatalogMigrationReceiptStore(tmp_path)
    first = build_provider_catalog_migration_receipt(
        _config(),
        routes,
        outcome="legacy_not_configured",
    )
    store.write(first)
    before = store.path.read_bytes()
    second = build_provider_catalog_migration_receipt(
        _config(),
        routes,
        outcome="catalog_present",
    )
    with pytest.raises(ProviderCatalogMigrationReceiptError, match="disagrees"):
        store.write(second)

    assert store.path.read_bytes() == before

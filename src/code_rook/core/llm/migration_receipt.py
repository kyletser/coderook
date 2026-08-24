from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from code_rook.core.config import LlmConfig
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.state_paths import (
    StatePathSecurityError,
    secure_state_subdirectory,
    secure_user_state_root,
)

_MIGRATION_ID = "provider-catalog-v1"
_RECEIPT_NAME = f"{_MIGRATION_ID}.receipt.json"
_SHA256_PATTERN = r"^[a-f0-9]{64}$"

ProviderCatalogMigrationStatus = Literal["pending", "complete", "invalid"]
ProviderCatalogMigrationOutcome = Literal[
    "migrated",
    "catalog_present",
    "legacy_not_configured",
]


class ProviderCatalogMigrationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    migration_id: Literal["provider-catalog-v1"] = "provider-catalog-v1"
    completed_at: datetime
    outcome: ProviderCatalogMigrationOutcome
    legacy_config_digest: str = Field(pattern=_SHA256_PATTERN)
    route_catalog_digest: str = Field(pattern=_SHA256_PATTERN)
    receipt_digest: str = Field(pattern=_SHA256_PATTERN)


class ProviderCatalogMigrationReceiptError(RuntimeError):
    pass


# 将无密钥结构编码成稳定 SHA-256 摘要
def _digest_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# 计算旧 LLM 配置的脱敏输入摘要且永不纳入凭据正文
def legacy_config_digest(config: LlmConfig) -> str:
    payload = {
        "provider": config.provider,
        "default_model": config.default_model,
        "router": config.router,
        "router_plan_route": config.router_plan_route,
        "router_act_route": config.router_act_route,
        "router_cost_budget": config.router_cost_budget,
        "router_cost_fallback": config.router_cost_fallback,
        "base_url": config.base_url,
        "api_key_env": config.api_key_env,
        "credential_overlay_names": sorted(config.credential_overlay),
    }
    return _digest_payload(payload)


# 计算不含凭据引用与 Doctor 明细的 Route Catalog 输出摘要
def route_catalog_digest(routes: RouteStore) -> str:
    configured = routes.list()
    active = routes.active()
    payload = {
        "routes": sorted(route.validation_digest() for route in configured),
        "active": active.validation_digest() if active is not None else None,
    }
    return _digest_payload(payload)


# 计算收据除自校验字段外的稳定完整性摘要
def _receipt_digest(receipt: ProviderCatalogMigrationReceipt) -> str:
    return _digest_payload(
        receipt.model_dump(
            mode="json",
            exclude={"receipt_digest"},
        )
    )


# 构造同时绑定旧配置输入与 Route Catalog 输出的迁移完成收据
def build_provider_catalog_migration_receipt(
    config: LlmConfig,
    routes: RouteStore,
    *,
    outcome: ProviderCatalogMigrationOutcome,
) -> ProviderCatalogMigrationReceipt:
    incomplete = ProviderCatalogMigrationReceipt(
        completed_at=datetime.now(UTC),
        outcome=outcome,
        legacy_config_digest=legacy_config_digest(config),
        route_catalog_digest=route_catalog_digest(routes),
        receipt_digest="0" * 64,
    )
    return incomplete.model_copy(
        update={"receipt_digest": _receipt_digest(incomplete)}
    )


class ProviderCatalogMigrationReceiptStore:
    # 初始化用户状态根下的 Provider Catalog 迁移收据路径
    def __init__(self, state_root: Path | None = None) -> None:
        self._state_root = (state_root or Path("~/.coderook")).expanduser().absolute()
        self.path = self._state_root / "migrations" / _RECEIPT_NAME

    # 验证状态根与收据父目录均为真实目录且按写入需求决定是否创建
    def _safe_parent(self, *, create: bool) -> Path:
        try:
            root = secure_user_state_root(self._state_root, create=create)
            return secure_state_subdirectory(root, "migrations", create=create)
        except (OSError, StatePathSecurityError, ValueError) as exc:
            raise ProviderCatalogMigrationReceiptError(
                "provider catalog migration receipt path is unsafe"
            ) from exc

    # 严格读取 schema、迁移身份与自校验摘要均有效的完成收据
    def load(self) -> ProviderCatalogMigrationReceipt:
        self._safe_parent(create=False)
        if self.path.is_symlink() or not self.path.is_file():
            raise ProviderCatalogMigrationReceiptError(
                "provider catalog migration receipt is not a regular file"
            )
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("receipt must be an object")
            version = raw.get("schema_version")
            if not isinstance(version, int) or isinstance(version, bool) or version != 1:
                raise ValueError("unsupported receipt schema version")
            receipt = ProviderCatalogMigrationReceipt.model_validate(raw)
        except (OSError, UnicodeError, ValueError, ValidationError) as exc:
            raise ProviderCatalogMigrationReceiptError(
                "provider catalog migration receipt is invalid"
            ) from exc
        expected = _receipt_digest(receipt)
        if not hmac.compare_digest(receipt.receipt_digest, expected):
            raise ProviderCatalogMigrationReceiptError(
                "provider catalog migration receipt integrity check failed"
            )
        return receipt

    # 只读区分待迁移、迁移完成与损坏收据且不移动或重写任何证据
    def inspect(self) -> ProviderCatalogMigrationStatus:
        try:
            self._safe_parent(create=False)
        except ProviderCatalogMigrationReceiptError:
            return "invalid"
        if not self.path.exists() and not self.path.is_symlink():
            return "pending"
        try:
            self.load()
        except ProviderCatalogMigrationReceiptError:
            return "invalid"
        return "complete"

    # 原子写入完成收据并拒绝覆盖损坏或未来版本的既有证据
    def write(
        self,
        receipt: ProviderCatalogMigrationReceipt,
    ) -> ProviderCatalogMigrationReceipt:
        if self.path.exists() or self.path.is_symlink():
            existing = self.load()
            if existing == receipt:
                return existing
            raise ProviderCatalogMigrationReceiptError(
                "existing Provider Catalog migration receipt disagrees with current state"
            )
        parent = self._safe_parent(create=True)
        os.chmod(parent, 0o700)
        temporary = parent / f".{_RECEIPT_NAME}.{secrets.token_hex(4)}.tmp"
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(receipt.model_dump_json(indent=2) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return receipt


# 只读返回 Provider Catalog 迁移收据的严格状态
def inspect_provider_catalog_migration(
    state_root: Path | None = None,
) -> ProviderCatalogMigrationStatus:
    return ProviderCatalogMigrationReceiptStore(state_root).inspect()

from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.config import LlmConfig
from code_rook.core.llm.credentials import CredentialStore
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


# 功能：验证切换 route 只改变选择，不覆盖任何另一 route 凭据
# 设计：保存两条独立 file ref，来回切换后分别解析并比较原始值
def test_registry_switches_routes_without_overwriting_credentials(tmp_path: Path) -> None:
    registry, routes, credentials = _registry(tmp_path)
    first_ref = credentials.save("first", "first-secret")
    second_ref = credentials.save("second", "second-secret")
    routes.add(
        get_route_preset("openai").model_copy(
            update={"id": "first", "credential_ref": first_ref}
        )
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

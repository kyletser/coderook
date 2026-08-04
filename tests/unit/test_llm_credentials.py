from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.config import LlmConfig
from code_rook.core.llm.credentials import (
    CredentialStore,
    llm_is_configured,
    resolve_api_key,
    save_api_key,
)


class _MemoryBackend:
    # 初始化内存 keyring 与可选写入故障
    def __init__(self, *, fail_write: bool = False) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_write = fail_write

    # 从内存 keyring 读取凭据
    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    # 向内存 keyring 保存凭据或模拟后端不可用
    def set_password(self, service: str, account: str, password: str) -> None:
        if self.fail_write:
            raise RuntimeError("backend unavailable")
        self.values[(service, account)] = password

    # 从内存 keyring 删除凭据
    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


# 功能：验证 Anthropic 与 OpenAI-compatible 的 API key 在同一凭据文件中独立保存
# 设计：依次写入两个 provider 后读取 JSON，并分别解析配置，防止切换 provider 时覆盖另一份密钥
def test_credentials_keep_provider_keys_independent(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    save_api_key("anthropic", "ant-key", path)
    save_api_key("openai_compatible", "openai-key", path)

    data = json.loads(path.read_text(encoding="utf-8"))

    assert data["api_keys"] == {
        "anthropic": "ant-key",
        "openai_compatible": "openai-key",
    }
    assert resolve_api_key(LlmConfig(provider="anthropic"), path) == "ant-key"
    assert (
        resolve_api_key(
            LlmConfig(provider="openai_compatible", api_key_env="OPENAI_API_KEY"),
            path,
        )
        == "openai-key"
    )


# 功能：验证显式环境变量中的 API key 优先于用户凭据文件
# 设计：给同一 provider 同时设置两种来源并断言环境值胜出，保持既有部署配置的最高优先级
def test_environment_api_key_overrides_saved_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "credentials.json"
    save_api_key("anthropic", "saved-key", path)
    monkeypatch.setenv("CUSTOM_ANTHROPIC_KEY", "environment-key")
    config = LlmConfig(provider="anthropic", api_key_env="CUSTOM_ANTHROPIC_KEY")

    assert resolve_api_key(config, path) == "environment-key"


# 功能：验证 OpenAI-compatible 缺少 endpoint 时即使有 key 也仍被判定为未配置
# 设计：先保存有效 key，再分别检查空地址和完整地址，覆盖首次启动引导的端点判定边界
def test_openai_configuration_requires_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    save_api_key("openai_compatible", "saved-key", path)
    incomplete = LlmConfig(
        provider="openai_compatible",
        default_model="model",
        base_url="",
        api_key_env="OPENAI_API_KEY",
    )
    complete = LlmConfig(
        provider="openai_compatible",
        default_model="model",
        base_url="https://example.test/v1/chat/completions",
        api_key_env="OPENAI_API_KEY",
    )

    assert llm_is_configured(incomplete, path) is False
    assert llm_is_configured(complete, path) is True


# 功能：验证四种内置 Provider 都能使用各自独立凭据完成配置判定
# 设计：逐个保存不同 Key 并使用官方 endpoint，覆盖新增 Provider 标识与凭据隔离
@pytest.mark.parametrize(
    ("provider", "api_key_env", "base_url"),
    [
        ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions"),
        ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions"),
        ("anthropic", "ANTHROPIC_API_KEY", ""),
        (
            "siliconflow",
            "SILICONFLOW_API_KEY",
            "https://api.siliconflow.cn/v1/chat/completions",
        ),
    ],
)
def test_builtin_provider_configuration_is_supported(
    tmp_path: Path,
    provider: str,
    api_key_env: str,
    base_url: str,
) -> None:
    path = tmp_path / "credentials.json"
    save_api_key(provider, f"{provider}-key", path)
    config = LlmConfig(
        provider=provider,
        default_model="model",
        base_url=base_url,
        api_key_env=api_key_env,
    )

    assert llm_is_configured(config, path) is True


# 功能：验证 route 凭据优先写入 OS keyring 并以引用解析
# 设计：注入内存后端，断言用户文件未创建且解析结果只报告 keyring 来源
def test_route_credential_prefers_keyring(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path, backend=_MemoryBackend())

    reference = store.save("route-a", "secret-a")
    resolved = store.resolve(reference)

    assert reference == "keyring:route-a"
    assert resolved.value == "secret-a"
    assert resolved.source == "keyring"
    assert not path.exists()


# 功能：验证 keyring 不可用时自动回退到权限收紧文件
# 设计：让后端写入抛错，检查 file ref、V2 文档和重新实例化后的解析结果
def test_route_credential_falls_back_to_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path, backend=_MemoryBackend(fail_write=True))

    reference = store.save("route-a", "secret-a")
    restored = CredentialStore(path, backend=_MemoryBackend()).resolve(reference)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert reference == "file:route-a"
    assert restored.value == "secret-a"
    assert restored.source == "file"
    assert payload["route_credentials"] == {"route-a": "secret-a"}


# 功能：验证两个 route 的凭据保存、切换解析和删除互不覆盖
# 设计：强制文件后端保存两项，只删除第一项后确认第二项仍能解析
def test_route_credentials_are_isolated_by_route(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    store = CredentialStore(path, backend=_MemoryBackend(fail_write=True))
    first_ref = store.save("first", "first-secret")
    second_ref = store.save("second", "second-secret")

    store.delete(first_ref)

    assert store.resolve(first_ref).source == "missing"
    assert store.resolve(second_ref).value == "second-secret"


# 功能：验证 env credential ref 仅报告 presence/source 且缺失时安全降级
# 设计：设置一次环境变量后删除，比较两次解析而不经过任何凭据文件
def test_route_credential_resolves_environment_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = CredentialStore(tmp_path / "credentials.json", backend=_MemoryBackend())
    monkeypatch.setenv("ROUTE_TEST_KEY", "environment-secret")

    present = store.resolve("env:ROUTE_TEST_KEY")
    monkeypatch.delenv("ROUTE_TEST_KEY")
    missing = store.resolve("env:ROUTE_TEST_KEY")

    assert present.value == "environment-secret" and present.source == "env"
    assert missing.value is None and missing.source == "missing"

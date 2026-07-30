from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.config import LlmConfig
from code_rook.core.llm.credentials import (
    llm_is_configured,
    resolve_api_key,
    save_api_key,
)


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

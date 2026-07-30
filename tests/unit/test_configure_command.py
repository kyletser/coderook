from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from kyle_claude.cli.commands import configure as configure_module
from kyle_claude.core.config import KyleConfig, LlmConfig


# 功能：验证写入 llm 配置时会替换旧表且保留前后其他 TOML 小节
# 设计：构造 llm 位于两个小节之间的文件，写入后用 tomllib 语义由 get_config 的底层格式保证并检查文本唯一性
def test_write_llm_config_preserves_other_sections(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[core]\nport = 7440\n\n[llm]\nprovider = "anthropic"\n\n[logging]\nlevel = "DEBUG"\n',
        encoding="utf-8",
    )
    config = LlmConfig(
        provider="openai_compatible",
        default_model="deepseek-test",
        base_url="https://example.test/v1/chat/completions",
        api_key_env="OPENAI_API_KEY",
    )

    configure_module.write_llm_config(config, path)
    content = path.read_text(encoding="utf-8")

    assert content.count("[llm]") == 1
    assert 'provider = "openai_compatible"' in content
    assert "[core]\nport = 7440" in content
    assert '[logging]\nlevel = "DEBUG"' in content


# 功能：验证交互配置能迁移 .env 明文 key 并保存完整 OpenAI-compatible 设置
# 设计：用固定输入函数模拟首次配置，检查 key 仅出现在凭据 JSON，.env 仅留下非敏感连接参数
def test_configure_migrates_dotenv_secret_to_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "KYLE_LLM_PROVIDER=openai_compatible\n"
        "KYLE_LLM_API_KEY_ENV=KYLE_LLM_API_KEY\n"
        "KYLE_LLM_API_KEY=old-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KYLE_LLM_API_KEY", "old-secret")
    inputs = iter(["2", "new-model", "https://example.test/v1/chat/completions"])
    current = KyleConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="",
            base_url="",
            api_key_env="KYLE_LLM_API_KEY",
        )
    )
    expected = KyleConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="new-model",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="KYLE_LLM_API_KEY",
        )
    )
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    result = configure_module.configure_llm(
        current,
        input_fn=lambda _prompt: next(inputs),
        secret_fn=lambda _prompt: "",
        config_path=config_path,
        credential_file=credential_path,
    )
    env_values = dotenv_values(dotenv_path)
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))

    assert result is expected
    assert env_values["KYLE_LLM_DEFAULT_MODEL"] == "new-model"
    assert env_values["KYLE_LLM_BASE_URL"] == "https://example.test/v1/chat/completions"
    assert "KYLE_LLM_API_KEY" not in env_values
    assert credentials["api_keys"]["openai_compatible"] == "old-secret"
    assert "old-secret" not in config_path.read_text(encoding="utf-8")


# 功能：验证 Anthropic-compatible 向导允许官方接口留空并接受自定义模型和新 key
# 设计：模拟选择 Anthropic、输入模型、留空 endpoint 与隐藏 key，检查 TOML 和凭据文件职责分离
def test_configure_anthropic_official_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    inputs = iter(["1", "claude-custom", ""])
    expected = KyleConfig()
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    configure_module.configure_llm(
        KyleConfig(),
        input_fn=lambda _prompt: next(inputs),
        secret_fn=lambda _prompt: "anthropic-secret",
        config_path=config_path,
        credential_file=credential_path,
    )

    assert 'provider = "anthropic"' in config_path.read_text(encoding="utf-8")
    assert 'base_url = ""' in config_path.read_text(encoding="utf-8")
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    assert credentials["api_keys"]["anthropic"] == "anthropic-secret"

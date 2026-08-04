from __future__ import annotations

import json
from pathlib import Path

import pytest
from dotenv import dotenv_values

from code_rook.cli.commands import configure as configure_module
from code_rook.core.config import CodeRookConfig, LlmConfig
from code_rook.core.llm.route_store import RouteStore


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
        "CODEROOK_LLM_PROVIDER=openai_compatible\n"
        "CODEROOK_LLM_API_KEY_ENV=CODEROOK_LLM_API_KEY\n"
        "CODEROOK_LLM_API_KEY=old-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEROOK_LLM_API_KEY", "old-secret")
    inputs = iter(["2", "new-model", "https://example.test/v1/chat/completions"])
    current = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="",
            base_url="",
            api_key_env="CODEROOK_LLM_API_KEY",
        )
    )
    expected = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="new-model",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="CODEROOK_LLM_API_KEY",
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
    assert env_values["CODEROOK_LLM_DEFAULT_MODEL"] == "new-model"
    assert env_values["CODEROOK_LLM_BASE_URL"] == "https://example.test/v1/chat/completions"
    assert "CODEROOK_LLM_API_KEY" not in env_values
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
    expected = CodeRookConfig()
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    configure_module.configure_llm(
        CodeRookConfig(),
        input_fn=lambda _prompt: next(inputs),
        secret_fn=lambda _prompt: "anthropic-secret",
        config_path=config_path,
        credential_file=credential_path,
    )

    assert 'provider = "anthropic"' in config_path.read_text(encoding="utf-8")
    assert 'base_url = ""' in config_path.read_text(encoding="utf-8")
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    assert credentials["api_keys"]["anthropic"] == "anthropic-secret"


# 功能：验证模型切换仅更新默认模型并保留当前 Provider、endpoint 和凭据引用
# 设计：使用自定义 OpenAI-compatible 配置和临时 TOML，比较保存文本与返回配置对象
def test_switch_llm_model_preserves_provider_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="old-model",
            router="static",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="CUSTOM_API_KEY",
        )
    )
    expected = CodeRookConfig(
        llm=LlmConfig(
            provider="openai_compatible",
            default_model="new-model",
            router="static",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="CUSTOM_API_KEY",
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    path = tmp_path / "config.toml"

    result = configure_module.switch_llm_model(
        current,
        " new-model ",
        config_path=path,
    )
    content = path.read_text(encoding="utf-8")

    assert result is expected
    assert 'default_model = "new-model"' in content
    assert 'provider = "openai_compatible"' in content
    assert 'base_url = "https://example.test/v1/chat/completions"' in content
    assert 'api_key_env = "CUSTOM_API_KEY"' in content


# 功能：验证内置 Provider 配置会保存固定 endpoint、模型和独立凭据
# 设计：选择 DeepSeek 并使用临时 TOML/凭据文件，排除用户 .env 和真实密钥影响
def test_save_provider_config_uses_builtin_preset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = CodeRookConfig(
        llm=LlmConfig(
            provider="deepseek",
            default_model="deepseek-v4-pro",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(configure_module, "get_config", lambda: expected)
    config_path = tmp_path / "config.toml"
    credential_path = tmp_path / "credentials.json"

    result = configure_module.save_provider_config(
        CodeRookConfig(),
        "deepseek",
        "deepseek-secret",
        "deepseek-v4-pro",
        config_path=config_path,
        credential_file=credential_path,
    )
    credentials = json.loads(credential_path.read_text(encoding="utf-8"))
    content = config_path.read_text(encoding="utf-8")

    assert result is expected
    assert credentials["api_keys"]["deepseek"] == "deepseek-secret"
    assert 'provider = "deepseek"' in content
    assert 'default_model = "deepseek-v4-pro"' in content
    assert 'base_url = "https://api.deepseek.com/chat/completions"' in content
    assert "deepseek-secret" not in content


# 功能：验证 coderook configure 将向导结果激活为显式 route 且不重启 Core
# 设计：替换交互向导和 RouteStore 边界，调用真实命令后检查活动 route 与模型
def test_cmd_configure_activates_route_without_core_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    updated = CodeRookConfig(
        llm=LlmConfig(
            provider="deepseek",
            default_model="deepseek-test",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
        )
    )
    monkeypatch.setattr(configure_module.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(configure_module, "configure_llm", lambda _config: updated)
    monkeypatch.setattr(configure_module, "RouteStore", lambda: routes)

    configure_module.cmd_configure(CodeRookConfig())

    active = routes.active()
    assert active is not None
    assert active.id == "legacy-deepseek"
    assert active.model == "deepseek-test"

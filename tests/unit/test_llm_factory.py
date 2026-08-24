from __future__ import annotations

import pytest
from pydantic import AnyHttpUrl

from code_rook.core.config import LlmConfig
from code_rook.core.llm import factory as factory_module
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.provider import AnthropicProvider
from code_rook.core.llm.routes import ProviderRoute


# 功能：验证 DeepSeek、OpenAI 和硅基流动都路由到 OpenAI-compatible 实现
# 设计：参数化内置 Provider 并注入固定凭据，额外断言只有 OpenAI 使用新 token 字段
@pytest.mark.parametrize(
    ("provider", "use_max_completion_tokens"),
    [
        ("deepseek", False),
        ("openai", True),
        ("siliconflow", False),
    ],
)
def test_factory_routes_builtin_openai_compatible_providers(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    use_max_completion_tokens: bool,
) -> None:
    monkeypatch.setattr(factory_module, "resolve_api_key", lambda _config: "test-key")
    config = LlmConfig(
        provider=provider,
        default_model="model",
        base_url="https://example.test/v1/chat/completions",
        api_key_env="TEST_API_KEY",
    )

    result = factory_module.create_llm_provider(config)

    assert isinstance(result, OpenAICompatibleProvider)
    assert result._use_max_completion_tokens is use_max_completion_tokens


# 功能：验证 Anthropic 内置接入仍使用原生 Messages API Provider
# 设计：注入固定凭据并检查实例类型，防止四平台统一配置时误走 Chat Completions
def test_factory_keeps_anthropic_native_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(factory_module, "resolve_api_key", lambda _config: "test-key")

    result = factory_module.create_llm_provider(
        LlmConfig(provider="anthropic", default_model="claude-sonnet-5")
    )

    assert isinstance(result, AnthropicProvider)


# 功能：验证旧 Provider 工厂可直接使用显式 env 文件的隐藏凭据覆盖
# 设计：不修改 os.environ 且不替换解析函数，创建真实兼容 Provider 以覆盖部署启动路径
def test_factory_consumes_explicit_env_credential_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)
    config = LlmConfig(
        provider="deepseek",
        default_model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/chat/completions",
        api_key_env="DEPLOYMENT_LLM_KEY",
        credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"},
    )

    result = factory_module.create_llm_provider(config)

    assert isinstance(result, OpenAICompatibleProvider)
    assert result._api_key == "explicit-file-secret"
    assert "explicit-file-secret" not in repr(config)


# 功能：验证 route factory 仅按 wire_format 选适配器，不读取模型名前缀
# 设计：给 Claude 名称配置 openai_chat、给 GPT 名称配置 anthropic_messages，断言协议仍服从 route
def test_route_factory_does_not_infer_wire_format_from_model_name() -> None:
    openai_wire = ProviderRoute(
        id="odd-openai",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("https://gateway.example/v1/chat/completions"),
        model="claude-looking-model",
        credential_ref="file:odd-openai",
        temperature=0.25,
    )
    anthropic_wire = ProviderRoute(
        id="odd-anthropic",
        provider="anthropic-compatible",
        wire_format="anthropic_messages",
        base_url=AnyHttpUrl("https://gateway.example"),
        model="gpt-looking-model",
        credential_ref="file:odd-anthropic",
        temperature=0.5,
    )

    openai_provider = factory_module.create_provider_for_route(openai_wire, "key-a")
    anthropic_provider = factory_module.create_provider_for_route(
        anthropic_wire,
        "key-b",
    )

    assert isinstance(openai_provider, OpenAICompatibleProvider)
    assert openai_provider._model == "claude-looking-model"
    assert openai_provider._temperature == 0.25
    assert isinstance(anthropic_provider, AnthropicProvider)
    assert anthropic_provider._model == "gpt-looking-model"
    assert anthropic_provider._temperature == 0.5

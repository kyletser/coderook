from __future__ import annotations

import pytest

from code_rook.core.config import LlmConfig
from code_rook.core.llm import factory as factory_module
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.provider import AnthropicProvider


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

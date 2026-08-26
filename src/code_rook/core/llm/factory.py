from __future__ import annotations

from code_rook.core.config import LlmConfig
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.credentials import normalize_provider, resolve_api_key
from code_rook.core.llm.experiment_budget import maybe_wrap_experiment_budget
from code_rook.core.llm.kinds import OPENAI_CHAT_PROVIDERS
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.openai_responses import OpenAIResponsesProvider
from code_rook.core.llm.provider import AnthropicProvider
from code_rook.core.llm.provider_presets import get_provider_preset
from code_rook.core.llm.routes import ProviderRoute


# 按 route 的显式 wire format 创建 Provider，绝不从模型 ID 推断协议
def create_provider_for_route(route: ProviderRoute, credential: str) -> LLMProvider:
    if route.wire_format == "anthropic_messages":
        provider: LLMProvider = AnthropicProvider(
            route.model,
            api_key=credential,
            base_url=str(route.base_url).rstrip("/"),
            context_window=route.context_window,
            thinking=route.thinking,
            supports_prompt_cache=route.supports_prompt_cache,
            temperature=route.temperature,
        )
    elif route.wire_format == "openai_chat":
        provider = OpenAICompatibleProvider(
            route.model,
            base_url=str(route.base_url).rstrip("/"),
            api_key_env="",
            api_key=credential,
            api_key_required=route.credential_required,
            use_max_completion_tokens=route.provider == "openai",
            context_window=route.context_window,
            thinking=route.thinking,
            temperature=route.temperature,
        )
    elif route.wire_format == "openai_responses":
        provider = OpenAIResponsesProvider(
            route.model,
            base_url=str(route.base_url).rstrip("/"),
            api_key=credential,
            api_key_required=route.credential_required,
            context_window=route.context_window,
            thinking=route.thinking,
            temperature=route.temperature,
        )
    else:
        raise SystemExit(f"Unsupported route wire format: {route.wire_format}")
    return maybe_wrap_experiment_budget(provider, model=route.model)


# 根据配置创建 provider，并从环境变量或用户凭据文件解析密钥
def create_llm_provider(config: LlmConfig) -> LLMProvider:
    provider = normalize_provider(config.provider)
    api_key = resolve_api_key(config)
    try:
        preset = get_provider_preset(provider)
        credential_required = preset.credential_required
    except ValueError:
        credential_required = True
    if credential_required and not api_key:
        raise SystemExit(
            f"{config.api_key_env} not set and no saved credential for {provider}; "
            "run `uv run coderook configure`"
        )
    if provider == "anthropic":
        resolved_provider: LLMProvider = AnthropicProvider(
            config.default_model,
            api_key=api_key,
            base_url=config.base_url,
        )
    elif provider in OPENAI_CHAT_PROVIDERS:
        resolved_provider = OpenAICompatibleProvider(
            config.default_model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            api_key=api_key or "",
            api_key_required=credential_required,
            use_max_completion_tokens=provider == "openai",
        )
    else:
        raise SystemExit(f"Unsupported LLM provider: {config.provider}")
    return maybe_wrap_experiment_budget(
        resolved_provider,
        model=config.default_model,
    )

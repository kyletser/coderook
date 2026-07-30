from __future__ import annotations

from code_rook.core.config import LlmConfig
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.credentials import normalize_provider, resolve_api_key
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.provider import AnthropicProvider


# 根据配置创建 provider，并从环境变量或用户凭据文件解析密钥
def create_llm_provider(config: LlmConfig) -> LLMProvider:
    provider = normalize_provider(config.provider)
    api_key = resolve_api_key(config)
    if not api_key:
        raise SystemExit(
            f"{config.api_key_env} not set and no saved credential for {provider}; "
            "run `uv run coderook configure`"
        )
    if provider == "anthropic":
        return AnthropicProvider(
            config.default_model,
            api_key=api_key,
            base_url=config.base_url,
        )
    if provider == "openai_compatible":
        return OpenAICompatibleProvider(
            config.default_model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            api_key=api_key,
        )
    raise SystemExit(f"Unsupported LLM provider: {config.provider}")

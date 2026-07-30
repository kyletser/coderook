from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class ProviderPreset:
    id: str
    name: str
    description: str
    chat_url: str
    models_url: str
    api_key_env: str
    preferred_models: tuple[str, ...]
    anthropic_api: bool = False
    model_params: tuple[tuple[str, str], ...] = ()


PROVIDER_PRESETS = (
    ProviderPreset(
        id="deepseek",
        name="DeepSeek API",
        description="DeepSeek 官方 API",
        chat_url="https://api.deepseek.com/chat/completions",
        models_url="https://api.deepseek.com/models",
        api_key_env="DEEPSEEK_API_KEY",
        preferred_models=("deepseek-v4-pro", "deepseek-v4-flash"),
    ),
    ProviderPreset(
        id="openai",
        name="OpenAI",
        description="OpenAI 官方 API",
        chat_url="https://api.openai.com/v1/chat/completions",
        models_url="https://api.openai.com/v1/models",
        api_key_env="OPENAI_API_KEY",
        preferred_models=("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic",
        description="Claude 官方 API",
        chat_url="",
        models_url="https://api.anthropic.com/v1/models",
        api_key_env="ANTHROPIC_API_KEY",
        preferred_models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
        anthropic_api=True,
    ),
    ProviderPreset(
        id="siliconflow",
        name="硅基流动",
        description="SiliconFlow OpenAI-compatible API",
        chat_url="https://api.siliconflow.cn/v1/chat/completions",
        models_url="https://api.siliconflow.cn/v1/models",
        api_key_env="SILICONFLOW_API_KEY",
        preferred_models=(
            "deepseek-ai/DeepSeek-V3.1-Terminus",
            "moonshotai/Kimi-K2-Instruct-0905",
            "Qwen/Qwen3-235B-A22B-Thinking-2507",
        ),
        model_params=(("type", "text"), ("sub_type", "chat")),
    ),
)

_PRESET_BY_ID = {preset.id: preset for preset in PROVIDER_PRESETS}
_OPENAI_EXCLUDED_PARTS = (
    "audio",
    "embedding",
    "image",
    "moderation",
    "realtime",
    "search",
    "transcribe",
    "tts",
    "whisper",
)


# 按稳定标识返回内置 Provider 配置
def get_provider_preset(provider: str) -> ProviderPreset:
    try:
        return _PRESET_BY_ID[provider]
    except KeyError as exc:
        raise ValueError(f"Unsupported built-in provider: {provider}") from exc


# 判断模型是否适用于当前 CodeRook 文本工具调用链路
def _supports_chat(provider: str, model: str) -> bool:
    lowered = model.lower()
    if provider == "anthropic":
        return lowered.startswith("claude-")
    if provider == "openai":
        return (
            lowered.startswith(("gpt-", "o1", "o3", "o4"))
            and not any(part in lowered for part in _OPENAI_EXCLUDED_PARTS)
        )
    return True


# 从 Provider 响应中提取、筛选并按推荐顺序排列模型 ID
def _parse_models(preset: ProviderPreset, payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError(f"{preset.name} returned an invalid model list")
    discovered = [
        str(item["id"])
        for item in payload["data"]
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and _supports_chat(preset.id, item["id"])
    ]
    preferred = [model for model in preset.preferred_models if model in discovered]
    remaining = sorted(set(discovered) - set(preferred), key=str.lower)
    models = preferred + remaining
    if not models:
        raise ValueError(f"{preset.name} returned no compatible chat models")
    return models


# 使用用户 API Key 查询该账号当前可用的聊天模型
async def discover_models(
    preset: ProviderPreset,
    api_key: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    key = api_key.strip()
    if not key:
        raise ValueError("API key cannot be empty")
    headers = {"Authorization": f"Bearer {key}"}
    if preset.anthropic_api:
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
    params = dict(preset.model_params)
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=20.0) as owned_client:
                response = await owned_client.get(
                    preset.models_url,
                    headers=headers,
                    params=params,
                )
        else:
            response = await client.get(
                preset.models_url,
                headers=headers,
                params=params,
            )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"{preset.name} authentication failed (HTTP {exc.response.status_code})"
        ) from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"Cannot connect to {preset.name}: {exc}") from exc
    return _parse_models(preset, response.json())

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
    provider_kind: str = "openai-compatible"
    wire_format: str = "openai_chat"
    credential_required: bool = True
    anthropic_api: bool = False
    model_params: tuple[tuple[str, str], ...] = ()
    supports_prompt_cache: bool = False
    supports_tools: bool = True
    supports_parallel_tools: bool = False
    supports_images: bool = False
    local_probe: bool = False
    aliases: tuple[str, ...] = ()

    @property
    # 返回创建 route 时使用的首选默认模型
    def default_model(self) -> str:
        return self.preferred_models[0]


@dataclass(frozen=True)
class LocalProviderProbeResult:
    provider_id: str
    reachable: bool
    host: str
    port: int
    reason: str


LocalPortConnector = Callable[[str, int, float], Awaitable[bool]]


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
        provider_kind="openai",
        supports_parallel_tools=True,
        supports_images=True,
    ),
    ProviderPreset(
        id="anthropic",
        name="Anthropic",
        description="Claude 官方 API",
        chat_url="https://api.anthropic.com",
        models_url="https://api.anthropic.com/v1/models",
        api_key_env="ANTHROPIC_API_KEY",
        preferred_models=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
        provider_kind="anthropic",
        wire_format="anthropic_messages",
        anthropic_api=True,
        supports_prompt_cache=True,
        supports_parallel_tools=True,
        supports_images=True,
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
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        description="Gemini OpenAI-compatible API",
        chat_url=("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        models_url="https://generativelanguage.googleapis.com/v1beta/openai/models",
        api_key_env="GEMINI_API_KEY",
        preferred_models=("gemini-3.7-flash", "gemini-3.1-pro-preview"),
        supports_images=True,
    ),
    ProviderPreset(
        id="moonshot",
        name="Kimi / Moonshot",
        description="Kimi Open Platform OpenAI-compatible API",
        chat_url="https://api.moonshot.ai/v1/chat/completions",
        models_url="https://api.moonshot.ai/v1/models",
        api_key_env="MOONSHOT_API_KEY",
        preferred_models=("kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-fast"),
        supports_images=True,
        aliases=("kimi",),
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        description="OpenRouter unified OpenAI-compatible API",
        chat_url="https://openrouter.ai/api/v1/chat/completions",
        models_url="https://openrouter.ai/api/v1/models",
        api_key_env="OPENROUTER_API_KEY",
        preferred_models=("openrouter/auto", "openrouter/free"),
        supports_images=True,
    ),
    ProviderPreset(
        id="ollama",
        name="Ollama",
        description="Local Ollama OpenAI-compatible API",
        chat_url="http://127.0.0.1:11434/v1/chat/completions",
        models_url="http://127.0.0.1:11434/v1/models",
        api_key_env="",
        preferred_models=("qwen3-coder",),
        credential_required=False,
        local_probe=True,
    ),
    ProviderPreset(
        id="lm-studio",
        name="LM Studio",
        description="Local LM Studio OpenAI-compatible API",
        chat_url="http://127.0.0.1:1234/v1/chat/completions",
        models_url="http://127.0.0.1:1234/v1/models",
        api_key_env="",
        preferred_models=("local-model",),
        credential_required=False,
        local_probe=True,
        aliases=("lm_studio",),
    ),
)

_PRESET_BY_ID = {
    alias: preset for preset in PROVIDER_PRESETS for alias in (preset.id, *preset.aliases)
}
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


# 按稳定标识或兼容别名返回统一 Provider 配置
def get_provider_preset(provider: str) -> ProviderPreset:
    normalized = provider.strip().lower().replace("_", "-")
    try:
        return _PRESET_BY_ID[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported built-in provider: {provider}") from exc


# 判断模型是否适用于当前 CodeRook 文本工具调用链路
def _supports_chat(provider: str, model: str) -> bool:
    lowered = model.lower()
    if provider == "anthropic":
        return lowered.startswith("claude-")
    if provider == "openai":
        return lowered.startswith(("gpt-", "o1", "o3", "o4")) and not any(
            part in lowered for part in _OPENAI_EXCLUDED_PARTS
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


# 使用用户 API Key 或本地免密端点查询当前可用的聊天模型
async def discover_models(
    preset: ProviderPreset,
    api_key: str = "",
    *,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    key = api_key.strip()
    if preset.credential_required and not key:
        raise ValueError("API key cannot be empty")
    headers: dict[str, str] = {}
    if key:
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


# 通过短连接探测本地端口并立即关闭，不发送模型请求或密钥
async def _connect_local_port(host: str, port: int, timeout_s: float) -> bool:
    writer: asyncio.StreamWriter | None = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout_s,
        )
        return True
    except (OSError, TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


# 对允许探测的 loopback Provider 执行可注入的轻量端口检查
async def probe_local_provider(
    provider: str | ProviderPreset,
    *,
    endpoint: str | None = None,
    connector: LocalPortConnector | None = None,
    timeout_s: float = 0.5,
) -> LocalProviderProbeResult:
    preset = get_provider_preset(provider) if isinstance(provider, str) else provider
    parsed = urlsplit(endpoint or preset.chat_url)
    host = parsed.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.endswith(".localhost")
    if not preset.local_probe or not loopback:
        raise ValueError(f"provider does not expose a local probe: {preset.id}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    reachable = await (connector or _connect_local_port)(host, port, timeout_s)
    return LocalProviderProbeResult(
        provider_id=preset.id,
        reachable=reachable,
        host=host,
        port=port,
        reason="local endpoint is reachable" if reachable else "local endpoint is unreachable",
    )

"""旧式 [llm] 配置的供应商白名单单一事实来源，供 credentials/factory/route_registry 共用。"""

from __future__ import annotations

from code_rook.core.llm.provider_presets import PROVIDER_PRESETS

_CATALOG_LEGACY_NAMES = frozenset(
    name.lower().replace("-", "_")
    for preset in PROVIDER_PRESETS
    for name in (preset.id, *preset.aliases)
)

# 旧式配置支持的全部供应商名（normalize_provider 归一化后的下划线形式）
SUPPORTED_LEGACY_PROVIDERS: frozenset[str] = frozenset(
    {*_CATALOG_LEGACY_NAMES, "openai_compatible"}
)

# 使用 OpenAI Chat Completions 线格式的供应商子集
OPENAI_CHAT_PROVIDERS: frozenset[str] = frozenset(
    {
        preset.id.replace("-", "_")
        for preset in PROVIDER_PRESETS
        if preset.wire_format == "openai_chat"
    }
    | {"kimi", "openai_compatible"}
)

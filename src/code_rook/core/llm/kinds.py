"""旧式 [llm] 配置的供应商白名单单一事实来源，供 credentials/factory/route_registry 共用。"""

from __future__ import annotations

# 旧式配置支持的全部供应商名（normalize_provider 归一化后的下划线形式）
SUPPORTED_LEGACY_PROVIDERS: frozenset[str] = frozenset(
    {"anthropic", "deepseek", "openai", "openai_compatible", "siliconflow"}
)

# 使用 OpenAI Chat Completions 线格式的供应商子集
OPENAI_CHAT_PROVIDERS: frozenset[str] = frozenset(
    {"deepseek", "openai", "openai_compatible", "siliconflow"}
)

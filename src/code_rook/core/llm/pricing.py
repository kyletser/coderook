from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# 用户级单价覆盖文件路径，测试或高级用户可通过环境变量重定向
_DEFAULT_PRICING_PATH = "~/.coderook/pricing.toml"
_BUILTIN_PRICING_EFFECTIVE_DATE = "2026-08-18"
_BUILTIN_PRICING_EFFECTIVE_DATES = {
    "qwen3.8-flash": "2026-08-27",
    "qwen-plus": "2026-09-11",
}


@dataclass(frozen=True)
class ModelPricing:
    # 每 1M token 的美元单价；cache_read/cache_write 为空表示该模型无缓存计价
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float = 0.0
    cache_write_per_m: float = 0.0


@dataclass(frozen=True)
class PricingQuote:
    pricing: ModelPricing
    source: str
    effective_date: str


# 内置参考单价（USD / 1M tokens）；仅为估算展示用，用户可用 pricing.toml 覆盖
_BUILTIN_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-6": ModelPricing(15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5": ModelPricing(3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-2": ModelPricing(1.0, 5.0, 0.1, 1.25),
    "gpt-5.6": ModelPricing(1.25, 10.0),
    "gpt-5.6-mini": ModelPricing(0.25, 2.0),
    "gpt-5.5": ModelPricing(1.25, 10.0),
    # Alibaba Cloud 标准实时推理美元价；地区或促销差异可由 pricing.toml 覆盖
    "qwen3.8-flash": ModelPricing(0.113, 0.382, 0.014, 0.177),
    # 北京/全球非思考 0-128K 档，沿用同目录报价的 7.08 CNY/USD 换算口径
    "qwen-plus": ModelPricing(0.113, 0.282),
    "deepseek-v4-flash": ModelPricing(0.14, 0.28, 0.0028),
    "deepseek-v4-pro": ModelPricing(0.435, 0.87, 0.003625),
}


# 返回用户级单价覆盖文件路径
def pricing_override_path() -> Path:
    value = os.environ.get("CODEROOK_PRICING", _DEFAULT_PRICING_PATH)
    return Path(value).expanduser()


# 解析 pricing.toml 覆盖；文件不存在返回空表，结构错误抛 ValueError
def load_pricing_overrides(path: Path | None = None) -> dict[str, ModelPricing]:
    target = path or pricing_override_path()
    if not target.exists():
        return {}
    try:
        data = tomllib.loads(target.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Invalid pricing file ({target}): {exc}") from exc
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"Invalid pricing file ({target}): [models] must be a table")
    overrides: dict[str, ModelPricing] = {}
    for name, raw in models.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid pricing entry: models.{name} must be a table")
        try:
            overrides[str(name)] = ModelPricing(
                input_per_m=float(raw["input"]),
                output_per_m=float(raw["output"]),
                cache_read_per_m=float(raw.get("cache_read", 0.0)),
                cache_write_per_m=float(raw.get("cache_write", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Invalid pricing entry models.{name}: needs numeric input/output"
            ) from exc
    return overrides


# 在指定价格表中按精确名称或带合法版本分隔符的最长前缀查找模型键
def _match_pricing_key(table: dict[str, ModelPricing], model: str) -> str | None:
    name = model.strip()
    if not name:
        return None
    if name in table:
        return name
    candidates = [
        key
        for key in table
        if name.startswith(key)
        and len(name) > len(key)
        and name[len(key)] in {"-", ".", ":", "/", "@"}
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


# 返回指定价格表中与模型名称匹配的单价
def _match_pricing(table: dict[str, ModelPricing], model: str) -> ModelPricing | None:
    key = _match_pricing_key(table, model)
    return table[key] if key is not None else None


# 返回单价查找结果：用户覆盖优先于内置，其次按最长前缀匹配日期后缀
def get_pricing(
    model: str,
    overrides: dict[str, ModelPricing] | None = None,
) -> ModelPricing | None:
    table = dict(_BUILTIN_PRICING)
    if overrides is not None:
        table.update(overrides)
    return _match_pricing(table, model)


# 返回带来源和生效日期的价格证据，供持久 receipt 离线解释成本
def resolve_pricing_quote(
    model: str,
    path: Path | None = None,
) -> PricingQuote | None:
    override_path = path or pricing_override_path()
    overrides = load_pricing_overrides(override_path)
    override = _match_pricing(overrides, model)
    if override is not None:
        modified = datetime.fromtimestamp(override_path.stat().st_mtime, tz=UTC).date()
        return PricingQuote(
            pricing=override,
            source=str(override_path),
            effective_date=modified.isoformat(),
        )
    builtin_key = _match_pricing_key(_BUILTIN_PRICING, model)
    if builtin_key is None:
        return None
    return PricingQuote(
        pricing=_BUILTIN_PRICING[builtin_key],
        source="builtin",
        effective_date=_BUILTIN_PRICING_EFFECTIVE_DATES.get(
            builtin_key,
            _BUILTIN_PRICING_EFFECTIVE_DATE,
        ),
    )


# 按 token 用量估算美元成本
def estimate_cost(
    pricing: ModelPricing,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    cost = 0.0
    cost += input_tokens * pricing.input_per_m / 1_000_000
    cost += output_tokens * pricing.output_per_m / 1_000_000
    cost += cache_read_tokens * pricing.cache_read_per_m / 1_000_000
    cost += cache_write_tokens * pricing.cache_write_per_m / 1_000_000
    return cost


# 估算缓存读相对全价输入的实际节省额
def cache_read_savings(
    pricing: ModelPricing,
    cache_read_tokens: int,
) -> float:
    discount = max(0.0, pricing.input_per_m - pricing.cache_read_per_m)
    return cache_read_tokens * discount / 1_000_000


# 把美元金额格式化为紧凑展示字符串
def format_cost(value: float) -> str:
    if value <= 0:
        return "$0"
    if value < 0.0001:
        return "<$0.0001"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"

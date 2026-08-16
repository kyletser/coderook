from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

# 用户级单价覆盖文件路径，测试或高级用户可通过环境变量重定向
_DEFAULT_PRICING_PATH = "~/.coderook/pricing.toml"


@dataclass(frozen=True)
class ModelPricing:
    # 每 1M token 的美元单价；cache_read/cache_write 为空表示该模型无缓存计价
    input_per_m: float
    output_per_m: float
    cache_read_per_m: float = 0.0
    cache_write_per_m: float = 0.0


# 内置参考单价（USD / 1M tokens）；仅为估算展示用，用户可用 pricing.toml 覆盖
_BUILTIN_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-6": ModelPricing(15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 0.3, 3.75),
    "claude-sonnet-4-5": ModelPricing(3.0, 15.0, 0.3, 3.75),
    "claude-haiku-4-2": ModelPricing(1.0, 5.0, 0.1, 1.25),
    "gpt-5.6": ModelPricing(1.25, 10.0),
    "gpt-5.6-mini": ModelPricing(0.25, 2.0),
    "gpt-5.5": ModelPricing(1.25, 10.0),
    "deepseek-v4": ModelPricing(0.27, 1.1),
    "deepseek-chat": ModelPricing(0.27, 1.1),
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


# 返回单价查找结果：用户覆盖优先于内置，其次按最长前缀匹配日期后缀
def get_pricing(
    model: str,
    overrides: dict[str, ModelPricing] | None = None,
) -> ModelPricing | None:
    table = dict(_BUILTIN_PRICING)
    if overrides is not None:
        table.update(overrides)
    name = model.strip()
    if not name:
        return None
    if name in table:
        return table[name]
    candidates = [key for key in table if name.startswith(key)]
    if not candidates:
        return None
    return table[max(candidates, key=len)]


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


# 估算缓存读相对全价输入的节省额
def cache_read_savings(
    pricing: ModelPricing,
    cache_read_tokens: int,
) -> float:
    return cache_read_tokens * pricing.input_per_m / 1_000_000


# 把美元金额格式化为紧凑展示字符串
def format_cost(value: float) -> str:
    if value <= 0:
        return "$0"
    if value < 0.0001:
        return "<$0.0001"
    if value < 1:
        return f"${value:.4f}"
    return f"${value:.2f}"

"""纯数据类控件参数与 App 返回类型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelSwitch:
    model: str
    session_id: str | None


@dataclass(frozen=True)
class ConfigSwitch:
    provider: str
    api_key: str = field(repr=False)
    model: str
    models: tuple[str, ...]
    session_id: str | None
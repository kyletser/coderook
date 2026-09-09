from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

DeliveryMode = Literal["one-at-a-time", "all"]


@dataclass(frozen=True)
class AgentDeliverySettings:
    steering_mode: DeliveryMode = "one-at-a-time"
    follow_up_mode: DeliveryMode = "one-at-a-time"


class AgentDeliverySettingsStore:
    # 绑定用户级交付设置文件
    def __init__(self, path: Path) -> None:
        self._path = path

    # 读取持久设置，文件不存在时使用当前配置作为首次默认值
    def load(self, defaults: AgentDeliverySettings) -> AgentDeliverySettings:
        if not self._path.exists():
            return defaults
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return defaults
        if not isinstance(payload, dict):
            return defaults
        steering = payload.get("steering_mode")
        follow_up = payload.get("follow_up_mode")
        if steering not in {"one-at-a-time", "all"} or follow_up not in {
            "one-at-a-time",
            "all",
        }:
            return defaults
        return AgentDeliverySettings(steering_mode=steering, follow_up_mode=follow_up)

    # 原子保存用户选择，供后续 Core 启动直接恢复
    def save(self, settings: AgentDeliverySettings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_temp = tempfile.mkstemp(
            dir=self._path.parent,
            prefix=f".{self._path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {"schema_version": 1, **asdict(settings)},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
        finally:
            temp_path.unlink(missing_ok=True)

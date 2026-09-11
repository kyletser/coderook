from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from code_rook.core.authority.models import WorkspaceTrust

logger = logging.getLogger(__name__)


# 将工作区路径归一化为跨重启稳定的本机键
def _workspace_key(workspace: Path) -> str:
    return os.path.normcase(str(workspace.expanduser().resolve(strict=False)))


class WorkspaceTrustStore:
    # 初始化用户级工作区信任存储
    def __init__(self, path: Path) -> None:
        self._path = path.expanduser().absolute()

    # 返回指定工作区的已保存信任决定，缺失或损坏时保持不信任
    def get(self, workspace: Path) -> WorkspaceTrust:
        return self._load().get(_workspace_key(workspace), WorkspaceTrust.UNTRUSTED)

    # 原子保存指定工作区的信任决定，撤销信任时删除对应记录
    def set(self, workspace: Path, trust: WorkspaceTrust) -> None:
        decisions = self._load()
        key = _workspace_key(workspace)
        if trust == WorkspaceTrust.TRUSTED:
            decisions[key] = trust
        else:
            decisions.pop(key, None)
        self._save(decisions)

    # 读取并验证持久化决定，格式错误时记录告警并回退到空集合
    def _load(self) -> dict[str, WorkspaceTrust]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            raw = payload.get("workspaces", {})
            if not isinstance(raw, dict):
                raise ValueError("workspaces must be an object")
            return {
                str(key): WorkspaceTrust(str(value))
                for key, value in raw.items()
                if isinstance(key, str)
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("workspace trust store is invalid; using untrusted defaults")
            return {}

    # 将完整信任映射写入同目录临时文件后原子替换
    def _save(self, decisions: dict[str, WorkspaceTrust]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "schema_version": 1,
            "workspaces": {
                key: value.value for key, value in sorted(decisions.items())
            },
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentProfile:
    name: str
    description: str
    system_prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    model: str = ""
    # 角色配置可选的访问限制策略，目前支持 "read_only"：仅允许副作用为 NONE 的工具
    restrict: str = ""


# 按两级优先级（项目本地 > 用户全局 > 内建）查找并解析角色配置
class AgentProfileLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 绑定项目根目录，确保从任意进程工作目录都能发现项目级 agent
    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = (project_root or Path.cwd()).resolve()

    # 查找指定角色配置；未找到返回 None
    def load(self, name: str) -> AgentProfile | None:
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return self._parse(path, name)
                except Exception:
                    return None
        return None

    # 返回 [项目本地, 用户全局, 内建] 路径；load() 返回第一个存在的，项目本地优先级最高
    def _search_paths(self, name: str) -> list[Path]:
        builtin = self._BUILTIN_DIR / f"{name}.toml"
        global_ = Path("~/.coderook/agents").expanduser() / f"{name}.toml"
        local = self._project_root / ".coderook" / "agents" / f"{name}.toml"
        return [local, global_, builtin]

    # 列出所有可用 agent 配置，按项目级、用户级、内建优先级合并同名项
    def list_all(self) -> list[AgentProfile]:
        profiles: dict[str, AgentProfile] = {}
        for directory in [
            self._BUILTIN_DIR,
            Path("~/.coderook/agents").expanduser(),
            self._project_root / ".coderook" / "agents",
        ]:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.toml")):
                try:
                    profiles[path.stem] = self._parse(path, path.stem)
                except Exception:
                    continue
        return [profiles[name] for name in sorted(profiles)]

    # 解析 TOML 角色配置文件
    def _parse(self, path: Path, name: str) -> AgentProfile:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        agent = data.get("agent", {})
        return AgentProfile(
            name=name,
            description=agent.get("description", ""),
            system_prompt=agent.get("system_prompt", "").strip(),
            allowed_tools=agent.get("allowed_tools", []),
            model=agent.get("model", ""),
            restrict=agent.get("restrict", ""),
        )

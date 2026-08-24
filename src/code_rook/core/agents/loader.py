from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

ProfileScope = Literal["builtin", "user", "project"]
ProfileTrust = Literal["builtin", "trusted", "untrusted"]
ProfileIntegrity = Literal["verified", "unmanaged"]
ProfileRestriction = Literal["", "read_only"]
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EMPTY_DIGEST = "sha256:" + "0" * 64
_ToolName = Annotated[str, Field(min_length=1, max_length=128)]
_MAX_PROFILE_BYTES = 512 * 1_024


class AgentProfileError(ValueError):
    pass


class AgentProfileTrustError(AgentProfileError):
    pass


class AgentProfileIntegrityError(AgentProfileError):
    pass


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(pattern=_PROFILE_NAME_RE.pattern)
    description: str = Field(default="", max_length=2_048)
    system_prompt: str = Field(default="", max_length=256 * 1_024)
    allowed_tools: list[_ToolName] = Field(default_factory=list, max_length=128)
    route: str = Field(default="", max_length=256)
    model: str = Field(default="", max_length=256)
    reasoning: Literal["", "low", "medium", "high", "xhigh", "max", "ultra"] = ""
    # 角色配置可选的访问限制策略，目前支持 "read_only"：仅允许副作用为 NONE 的工具
    restrict: ProfileRestriction = ""
    source: str = Field(default="manual", max_length=4_096)
    scope: ProfileScope = "project"
    trust: ProfileTrust = "untrusted"
    digest: str = Field(default=_EMPTY_DIGEST, pattern=r"^sha256:[a-f0-9]{64}$")
    integrity: ProfileIntegrity = "unmanaged"


class _AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    description: str = Field(default="", max_length=2_048)
    system_prompt: str = Field(default="", max_length=256 * 1_024)
    allowed_tools: list[_ToolName] = Field(default_factory=list, max_length=128)
    route: str = Field(default="", max_length=256)
    model: str = Field(default="", max_length=256)
    reasoning: Literal["", "low", "medium", "high", "xhigh", "max", "ultra"] = ""
    restrict: ProfileRestriction = ""

    # 去除 TOML 多行字符串首尾空白，保持旧 AgentProfile 行为
    @field_validator("system_prompt")
    @classmethod
    def _strip_system_prompt(cls, value: str) -> str:
        return value.strip()


class _AgentDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent: _AgentConfig


# 按两级优先级（项目本地 > 用户全局 > 内建）查找并解析角色配置
class AgentProfileLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 绑定项目根目录，确保从任意进程工作目录都能发现项目级 agent
    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = (project_root or Path.cwd()).resolve()

    # 查找指定角色配置；未找到返回 None
    def load(self, name: str) -> AgentProfile | None:
        if _PROFILE_NAME_RE.fullmatch(name) is None:
            return None
        for path in self._search_paths(name):
            if path.exists():
                try:
                    return self._parse(path, name)
                except (AgentProfileError, OSError):
                    return None
        return None

    # 加载可执行 profile，并按冻结 workspace trust 与发现 digest 执行 fail-closed 校验
    def load_for_execution(
        self,
        name: str,
        *,
        workspace_trusted: bool,
        expected_digest: str | None = None,
    ) -> AgentProfile | None:
        if _PROFILE_NAME_RE.fullmatch(name) is None:
            raise AgentProfileError(f"invalid agent profile name: {name!r}")
        trust_error: AgentProfileTrustError | None = None
        for path in self._search_paths(name):
            if not path.is_file():
                continue
            scope = self._scope_for(path)
            if scope == "project" and not workspace_trusted:
                trust_error = AgentProfileTrustError(
                    f"agent profile is not trusted for execution: {name} scope=project"
                )
                continue
            self._validate_source(path)
            profile = self._parse(path, name)
            if expected_digest is not None and profile.digest != expected_digest:
                raise AgentProfileIntegrityError(
                    f"agent profile digest changed after discovery: {name} "
                    f"expected={expected_digest} actual={profile.digest}"
                )
            return profile
        if trust_error is not None:
            raise trust_error
        return None

    # 返回 [项目本地, 用户全局, 内建] 路径；load() 返回第一个存在的，项目本地优先级最高
    def _search_paths(self, name: str) -> list[Path]:
        builtin = self._BUILTIN_DIR / f"{name}.toml"
        global_ = Path("~/.coderook/agents").expanduser() / f"{name}.toml"
        local = self._project_root / ".coderook" / "agents" / f"{name}.toml"
        return [local, global_, builtin]

    # 验证 profile 候选是固定 agents 目录中的普通文件且不经符号链接跳转
    def _validate_source(self, path: Path) -> None:
        scope = self._scope_for(path)
        expected_parent = {
            "builtin": self._BUILTIN_DIR,
            "user": Path("~/.coderook/agents").expanduser(),
            "project": self._project_root / ".coderook" / "agents",
        }[scope]
        containment_root = {
            "builtin": self._BUILTIN_DIR.parent,
            "user": Path("~").expanduser(),
            "project": self._project_root,
        }[scope]
        if expected_parent.is_symlink() or path.is_symlink():
            raise AgentProfileError(
                f"agent profile source must not be a symbolic link: {path}"
            )
        try:
            parent_relative = expected_parent.relative_to(containment_root)
        except ValueError as exc:
            raise AgentProfileError(
                f"agent profile directory escapes declared root: {expected_parent}"
            ) from exc
        cursor = containment_root
        for part in parent_relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise AgentProfileError(
                    f"agent profile directory traverses a symbolic link: {cursor}"
                )
        if path.parent != expected_parent:
            raise AgentProfileError(f"agent profile source escapes declared directory: {path}")
        if not expected_parent.resolve().is_relative_to(containment_root.resolve()):
            raise AgentProfileError(
                f"agent profile directory escapes declared root: {expected_parent}"
            )
        if path.resolve().parent != expected_parent.resolve():
            raise AgentProfileError(f"agent profile source escapes declared directory: {path}")

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
                except (AgentProfileError, OSError):
                    profiles.pop(path.stem, None)
                    continue
        return [profiles[name] for name in sorted(profiles)]

    # 列出可进入当前 Turn 模型上下文的 profile，并让调用方冻结返回 digest
    def list_for_execution(self, *, workspace_trusted: bool) -> list[AgentProfile]:
        names: set[str] = set()
        directories = [
            self._BUILTIN_DIR,
            Path("~/.coderook/agents").expanduser(),
        ]
        if workspace_trusted:
            directories.append(self._project_root / ".coderook" / "agents")
        for directory in directories:
            if not directory.is_dir() or directory.is_symlink():
                continue
            names.update(
                path.stem
                for path in directory.glob("*.toml")
                if _PROFILE_NAME_RE.fullmatch(path.stem) is not None
            )
        ready: list[AgentProfile] = []
        for name in sorted(names):
            try:
                profile = self.load_for_execution(
                    name,
                    workspace_trusted=workspace_trusted,
                )
            except AgentProfileError:
                continue
            if profile is not None:
                ready.append(profile)
        return ready

    # 解析 TOML 角色配置文件
    def _parse(self, path: Path, name: str) -> AgentProfile:
        if _PROFILE_NAME_RE.fullmatch(name) is None:
            raise AgentProfileError(f"invalid agent profile name: {name!r}")
        if path.is_symlink():
            raise AgentProfileError(
                f"agent profile source must not be a symbolic link: {path}"
            )
        try:
            if path.stat().st_size > _MAX_PROFILE_BYTES:
                raise AgentProfileError(
                    f"agent profile exceeds {_MAX_PROFILE_BYTES} bytes: {path}"
                )
            raw = path.read_bytes()
            data = tomllib.loads(raw.decode("utf-8"))
            agent = _AgentDocument.model_validate(data).agent
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise AgentProfileError(f"invalid agent profile {path}: {exc}") from exc
        scope = self._scope_for(path)
        trust: ProfileTrust = (
            "builtin" if scope == "builtin" else "trusted" if scope == "user" else "untrusted"
        )
        return AgentProfile(
            name=name,
            description=agent.description,
            system_prompt=agent.system_prompt,
            allowed_tools=agent.allowed_tools,
            route=agent.route,
            model=agent.model,
            reasoning=agent.reasoning,
            restrict=agent.restrict,
            source=str(path.resolve()),
            scope=scope,
            trust=trust,
            digest=f"sha256:{hashlib.sha256(raw).hexdigest()}",
            integrity="verified" if scope == "builtin" else "unmanaged",
        )

    # 根据固定搜索根判断 profile 来源，任意外部测试路径按 project/untrusted 处理
    def _scope_for(self, path: Path) -> ProfileScope:
        parent = path.resolve().parent
        if parent == self._BUILTIN_DIR.resolve():
            return "builtin"
        if parent == Path("~/.coderook/agents").expanduser().resolve():
            return "user"
        return "project"

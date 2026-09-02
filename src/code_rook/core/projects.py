from __future__ import annotations

import hashlib
import json
import os
import string
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_PROJECTS_FILE = "projects.json"
_PROJECTS_DIRECTORY = "CodeRookProjects"
_WELCOME_WORKSPACE = "welcome-workspace"
_INVALID_NAME_CHARS = set('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class ProjectRecord:
    id: str
    name: str
    root: str
    kind: str
    created_at: float
    last_opened_at: float


class ProjectRegistry:
    # 初始化用户级项目注册表并固定默认空白项目目录
    def __init__(
        self,
        state_root: Path | None = None,
        default_projects_root: Path | None = None,
    ) -> None:
        self._state_root = (state_root or Path("~/.coderook")).expanduser().absolute()
        self._path = self._state_root / _PROJECTS_FILE
        self._default_projects_root = (
            default_projects_root or Path.home() / _PROJECTS_DIRECTORY
        ).expanduser().absolute()

    @property
    # 返回新建空白项目的默认父目录
    def default_projects_root(self) -> Path:
        return self._default_projects_root

    @property
    # 返回未选择用户项目时使用的隔离欢迎工作区
    def welcome_workspace(self) -> Path:
        return self._state_root / _WELCOME_WORKSPACE

    # 创建并返回不包含任何 Agent 源码的隔离欢迎工作区
    def prepare_welcome_workspace(self) -> Path:
        self._state_root.mkdir(parents=True, exist_ok=True)
        self.welcome_workspace.mkdir(parents=False, exist_ok=True)
        return self.welcome_workspace.resolve(strict=True)

    # 判断目录是内部欢迎区或包含当前运行的 CodeRook 源码
    def is_protected_workspace(self, root: Path) -> bool:
        try:
            resolved = root.expanduser().resolve(strict=False)
        except OSError:
            return False
        if os.path.normcase(str(resolved)) == os.path.normcase(
            str(self.welcome_workspace.resolve(strict=False))
        ):
            return True
        source_checkout = _running_source_checkout()
        return source_checkout is not None and (
            source_checkout.is_relative_to(resolved)
            or resolved.is_relative_to(source_checkout)
        )

    # 读取注册表并忽略已不存在的项目目录
    def list_projects(self) -> list[ProjectRecord]:
        records = [
            record
            for record in self._load()
            if Path(record.root).is_dir()
            and not self.is_protected_workspace(Path(record.root))
        ]
        return sorted(records, key=lambda item: item.last_opened_at, reverse=True)

    # 注册已有目录或刷新同路径项目的最近打开时间
    def register(
        self,
        root: Path,
        *,
        name: str | None = None,
        kind: str = "existing",
    ) -> ProjectRecord:
        resolved = self._existing_directory(root)
        if self.is_protected_workspace(resolved):
            raise ValueError("CodeRook's internal source or welcome workspace cannot be opened")
        now = time.time()
        records = self._load()
        project_id = self._project_id(resolved)
        existing = next((item for item in records if item.id == project_id), None)
        record = ProjectRecord(
            id=project_id,
            name=(name or (existing.name if existing else resolved.name) or str(resolved)),
            root=str(resolved),
            kind=existing.kind if existing is not None else kind,
            created_at=existing.created_at if existing is not None else now,
            last_opened_at=now,
        )
        self._save([item for item in records if item.id != project_id] + [record])
        return record

    # 在默认或用户选择的父目录下创建全新空白项目并加入注册表
    def create_blank(self, name: str, parent: Path | None = None) -> ProjectRecord:
        clean_name = self._validate_name(name)
        base = (parent or self._default_projects_root).expanduser().absolute()
        base.mkdir(parents=True, exist_ok=True)
        if not base.is_dir():
            raise ValueError("project parent is not a directory")
        target = base / clean_name
        if self.is_protected_workspace(target):
            raise ValueError("CodeRook's internal source cannot be used as a project")
        target.mkdir(parents=False, exist_ok=False)
        return self.register(target, name=clean_name, kind="blank")

    # 从注册表移除项目记录但绝不删除用户目录或文件
    def forget(self, project_id: str) -> None:
        records = self._load()
        if not any(item.id == project_id for item in records):
            raise ValueError("project is not registered")
        self._save([item for item in records if item.id != project_id])

    # 按稳定 ID 查询仍存在的项目目录
    def get(self, project_id: str) -> ProjectRecord:
        for record in self._load():
            if (
                record.id == project_id
                and Path(record.root).is_dir()
                and not self.is_protected_workspace(Path(record.root))
            ):
                return record
        raise ValueError("project is not available")

    # 列出本机目录选择器的驱动器或指定目录的直接子目录
    def browse(self, path: str | None = None) -> dict[str, Any]:
        if not path:
            roots = self._filesystem_roots()
            return {
                "path": "",
                "parent": None,
                "roots": [str(item) for item in roots],
                "directories": [],
            }
        current = self._existing_directory(Path(path))
        directories: list[dict[str, str]] = []
        try:
            children = sorted(
                (item for item in current.iterdir() if item.is_dir()),
                key=lambda item: item.name.casefold(),
            )
        except OSError as exc:
            raise ValueError("directory cannot be read") from exc
        for child in children[:500]:
            try:
                resolved = child.resolve(strict=True)
            except OSError:
                continue
            directories.append({"name": child.name, "path": str(resolved)})
        parent = current.parent if current.parent != current else None
        return {
            "path": str(current),
            "parent": str(parent) if parent is not None else None,
            "roots": [],
            "directories": directories,
        }

    # 将项目名称限制为一个跨平台安全的目录组件
    def _validate_name(self, name: str) -> str:
        candidate = name.strip()
        if not candidate or candidate in {".", ".."}:
            raise ValueError("project name is required")
        if len(candidate) > 80:
            raise ValueError("project name is too long")
        if any(char in _INVALID_NAME_CHARS or ord(char) < 32 for char in candidate):
            raise ValueError("project name contains invalid characters")
        if candidate[-1] in {".", " "}:
            raise ValueError("project name cannot end with a dot or space")
        stem = candidate.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError("project name is reserved by Windows")
        return candidate

    # 解析并确认用户选中的路径确实是本机目录
    def _existing_directory(self, path: Path) -> Path:
        try:
            resolved = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError("project directory does not exist") from exc
        if not resolved.is_dir():
            raise ValueError("project path is not a directory")
        return resolved

    # 生成大小写归一化路径的稳定项目标识
    def _project_id(self, root: Path) -> str:
        normalized = os.path.normcase(str(root))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]

    # 返回 Windows 驱动器或 POSIX 根目录供目录浏览器起步
    def _filesystem_roots(self) -> list[Path]:
        if os.name != "nt":
            return [Path("/")]
        roots: list[Path] = []
        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.is_dir():
                roots.append(drive)
        return roots

    # 从 JSON 文件读取合法项目记录，损坏文件给出明确错误
    def _load(self) -> list[ProjectRecord]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            raw_records = payload.get("projects", [])
            if not isinstance(raw_records, list):
                raise ValueError
            return [ProjectRecord(**item) for item in raw_records if isinstance(item, dict)]
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("project registry is invalid") from exc

    # 原子写入项目注册表，避免进程中断留下半个 JSON
    def _save(self, records: list[ProjectRecord]) -> None:
        self._state_root.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        payload = {
            "schema_version": 1,
            "projects": [asdict(record) for record in records],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self._path)


# 定位当前可编辑安装对应的 CodeRook 源码根，普通 wheel 安装返回 None
def _running_source_checkout() -> Path | None:
    candidate = Path(__file__).resolve().parents[3]
    if (
        (candidate / ".git").is_dir()
        and (candidate / "pyproject.toml").is_file()
        and (candidate / "src" / "code_rook").is_dir()
    ):
        return candidate
    return None

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class StatePathSecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserStateLayout:
    root: Path
    sessions: Path
    goals: Path
    backups: Path
    migrations: Path
    runtime_database: Path


# 验证用户状态根不是符号链接或普通文件，并按需安全创建目录
def secure_user_state_root(root: Path, *, create: bool) -> Path:
    candidate = root.expanduser().absolute()
    if os.path.lexists(candidate) and (
        candidate.is_symlink() or not candidate.is_dir()
    ):
        raise StatePathSecurityError(
            "CodeRook user state root must be a real directory"
        )
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if not candidate.exists():
        return candidate
    resolved = candidate.resolve(strict=True)
    if candidate.is_symlink() or not resolved.is_dir():
        raise StatePathSecurityError(
            "CodeRook user state root changed during validation"
        )
    return candidate


# 验证状态根下的固定子目录不会通过符号链接越界并按需创建
def secure_state_subdirectory(root: Path, name: str, *, create: bool) -> Path:
    if not name or Path(name).name != name:
        raise ValueError("state subdirectory name must be a single path component")
    state_root = secure_user_state_root(root, create=create)
    path = state_root / name
    if os.path.lexists(path) and (path.is_symlink() or not path.is_dir()):
        raise StatePathSecurityError(
            f"CodeRook {name} state path must be a real directory"
        )
    if create:
        path.mkdir(parents=False, exist_ok=True)
    if path.exists() and (
        path.is_symlink()
        or not path.resolve(strict=True).is_relative_to(
            state_root.resolve(strict=True)
        )
    ):
        raise StatePathSecurityError(
            f"CodeRook {name} state path crosses the user state boundary"
        )
    return path


# 验证状态根的关键可写目录与 Runtime 数据库均不能通过符号链接越界
def prepare_user_state_layout(root: Path | None = None) -> UserStateLayout:
    state_root = secure_user_state_root(
        root or Path("~/.coderook"),
        create=True,
    )
    resolved_root = state_root.resolve(strict=True)
    directories: dict[str, Path] = {}
    for name in ("sessions", "goals", "backups", "migrations"):
        directories[name] = secure_state_subdirectory(
            state_root,
            name,
            create=True,
        )
    runtime_database = state_root / "runtime.db"
    if os.path.lexists(runtime_database) and (
        runtime_database.is_symlink() or not runtime_database.is_file()
    ):
        raise StatePathSecurityError(
            "CodeRook runtime database must be a real file inside the user state root"
        )
    if not runtime_database.parent.resolve(strict=True).is_relative_to(resolved_root):
        raise StatePathSecurityError(
            "CodeRook runtime database parent crosses the user state boundary"
        )
    return UserStateLayout(
        root=state_root,
        sessions=directories["sessions"],
        goals=directories["goals"],
        backups=directories["backups"],
        migrations=directories["migrations"],
        runtime_database=runtime_database,
    )

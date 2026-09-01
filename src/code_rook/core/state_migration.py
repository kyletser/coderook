from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_LEGACY_STATE_NAME = ".kyle"
_CURRENT_STATE_NAME = ".coderook"
_MIGRATION_MARKER = ".brand-migration-v1.json"
_PROJECT_SKIP_NAMES = {"worktrees"}


@dataclass(frozen=True)
class MigrationReport:
    user_files_copied: int = 0
    project_files_copied: int = 0
    legacy_user_state_found: bool = False
    legacy_project_state_found: bool = False


class MigrationSecurityError(RuntimeError):
    pass


# 验证迁移目标仍位于固定根目录内且任一已存在层级都不是符号链接
def _prepare_target_directory(target: Path, root: Path) -> None:
    if root.is_symlink() or target.is_symlink():
        raise MigrationSecurityError(f"migration target must not be a symlink: {target}")
    resolved_root = root.resolve(strict=False)
    resolved_target = target.resolve(strict=False)
    if not resolved_target.is_relative_to(resolved_root):
        raise MigrationSecurityError(f"migration target escaped state root: {target}")
    current = root
    relative = target.relative_to(root)
    for part in relative.parts:
        if current.is_symlink():
            raise MigrationSecurityError(
                f"migration target crosses a symlink: {current}"
            )
        current /= part
    if current.is_symlink():
        raise MigrationSecurityError(f"migration target crosses a symlink: {current}")
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.resolve().is_relative_to(resolved_root):
        raise MigrationSecurityError(f"migration target changed during setup: {target}")


# 以排他创建复制单个普通文件，拒绝最终目标 symlink 与覆盖竞态
def _copy_new_file(source: Path, destination: Path, root: Path) -> bool:
    if source.is_symlink() or not source.is_file():
        return False
    _prepare_target_directory(destination.parent, root)
    if destination.is_symlink():
        raise MigrationSecurityError(
            f"migration destination must not be a symlink: {destination}"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags, 0o600)
    except FileExistsError:
        if destination.is_symlink():
            raise MigrationSecurityError(
                f"migration destination became a symlink: {destination}"
            ) from None
        return False
    try:
        with os.fdopen(descriptor, "wb") as output_stream, source.open(
            "rb"
        ) as input_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if source.is_symlink():
        destination.unlink(missing_ok=True)
        raise MigrationSecurityError(f"migration source changed to a symlink: {source}")
    return True


# 将来源目录中的缺失文件复制到受控目标，跳过来源 symlink 和指定目录
def _copy_missing_tree(
    source: Path,
    target: Path,
    skip_names: set[str],
    *,
    root: Path,
) -> int:
    copied = 0
    _prepare_target_directory(target, root)
    for child in source.iterdir():
        if child.name in skip_names or child.is_symlink():
            continue
        destination = target / child.name
        if child.is_dir():
            copied += _copy_missing_tree(child, destination, skip_names, root=root)
        elif _copy_new_file(child, destination, root):
            copied += 1
    return copied


# 写入不含用户内容的迁移回执，后续启动可据此审计迁移结果
def _write_marker(target: Path, report: MigrationReport) -> None:
    _prepare_target_directory(target, target)
    marker = target / _MIGRATION_MARKER
    if marker.is_symlink():
        raise MigrationSecurityError(f"migration marker must not be a symlink: {marker}")
    if marker.exists():
        return
    payload = {
        "version": 1,
        "migrated_at": datetime.now(UTC).isoformat(),
        **asdict(report),
    }
    try:
        with marker.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=True, indent=2) + "\n")
    except FileExistsError:
        if marker.is_symlink():
            raise MigrationSecurityError(
                f"migration marker became a symlink: {marker}"
            ) from None


# 自动迁移可信用户状态；项目状态只有调用方显式确认时才增量复制
def migrate_legacy_state(
    *,
    user_home: Path | None = None,
    workspace: Path | None = None,
    include_project: bool = False,
) -> MigrationReport:
    home = (user_home or Path.home()).resolve()
    project = (workspace or Path.cwd()).resolve()
    legacy_user = home / _LEGACY_STATE_NAME
    current_user = home / _CURRENT_STATE_NAME
    legacy_project = project / _LEGACY_STATE_NAME
    current_project = project / _CURRENT_STATE_NAME

    legacy_user_found = legacy_user.is_dir() and not legacy_user.is_symlink()
    legacy_project_found = legacy_project.is_dir() and not legacy_project.is_symlink()
    user_files = (
        _copy_missing_tree(legacy_user, current_user, set(), root=current_user)
        if legacy_user_found
        else 0
    )
    project_files = (
        _copy_missing_tree(
            legacy_project,
            current_project,
            _PROJECT_SKIP_NAMES,
            root=current_project,
        )
        if include_project and legacy_project_found
        else 0
    )
    report = MigrationReport(
        user_files_copied=user_files,
        project_files_copied=project_files,
        legacy_user_state_found=legacy_user_found,
        legacy_project_state_found=legacy_project_found,
    )
    if legacy_user_found:
        _write_marker(current_user, report)
    if include_project and legacy_project_found:
        _write_marker(current_project, report)
    return report


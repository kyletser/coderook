from __future__ import annotations

import json
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


# 将来源目录中的缺失文件复制到目标目录，跳过符号链接和指定目录
def _copy_missing_tree(source: Path, target: Path, skip_names: set[str]) -> int:
    copied = 0
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.name in skip_names or child.is_symlink():
            continue
        destination = target / child.name
        if child.is_dir():
            copied += _copy_missing_tree(child, destination, set())
        elif child.is_file() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)
            copied += 1
    return copied


# 写入不含用户内容的迁移回执，后续启动可据此审计迁移结果
def _write_marker(target: Path, report: MigrationReport) -> None:
    target.mkdir(parents=True, exist_ok=True)
    marker = target / _MIGRATION_MARKER
    if marker.exists():
        return
    payload = {
        "version": 1,
        "migrated_at": datetime.now(UTC).isoformat(),
        **asdict(report),
    }
    marker.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


# 将旧品牌的用户与项目状态增量迁移到 CodeRook 路径且不覆盖现有文件
def migrate_legacy_state(
    *,
    user_home: Path | None = None,
    workspace: Path | None = None,
) -> MigrationReport:
    home = (user_home or Path.home()).resolve()
    project = (workspace or Path.cwd()).resolve()
    legacy_user = home / _LEGACY_STATE_NAME
    current_user = home / _CURRENT_STATE_NAME
    legacy_project = project / _LEGACY_STATE_NAME
    current_project = project / _CURRENT_STATE_NAME

    legacy_user_found = legacy_user.is_dir()
    legacy_project_found = legacy_project.is_dir()
    user_files = (
        _copy_missing_tree(legacy_user, current_user, set())
        if legacy_user_found
        else 0
    )
    project_files = (
        _copy_missing_tree(legacy_project, current_project, _PROJECT_SKIP_NAMES)
        if legacy_project_found
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
    if legacy_project_found:
        _write_marker(current_project, report)
    return report


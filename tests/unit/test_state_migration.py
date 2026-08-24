from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from code_rook.core.state_migration import migrate_legacy_state


# 功能：验证旧用户状态会复制到 CodeRook 目录且保留嵌套会话文件
# 设计：使用隔离 home 构造旧目录，避免触碰开发机真实数据，并检查迁移回执不包含文件正文
def test_migrates_legacy_user_state_without_losing_sessions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    legacy = home / ".kyle"
    session = legacy / "sessions" / "sess-1" / "thread.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text('{"role":"user","content":"remember me"}\n', encoding="utf-8")

    report = migrate_legacy_state(user_home=home, workspace=workspace)

    migrated = home / ".coderook" / "sessions" / "sess-1" / "thread.jsonl"
    marker = home / ".coderook" / ".brand-migration-v1.json"
    assert migrated.read_text(encoding="utf-8") == session.read_text(encoding="utf-8")
    assert report.user_files_copied == 1
    assert report.legacy_user_state_found is True
    assert "remember me" not in marker.read_text(encoding="utf-8")


# 功能：验证迁移不会覆盖 CodeRook 目录中已经存在的新配置
# 设计：让新旧目录包含同名文件并断言新值保持不变，覆盖增量迁移最重要的数据安全边界
def test_migration_never_overwrites_current_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    legacy_config = home / ".kyle" / "config.toml"
    current_config = home / ".coderook" / "config.toml"
    legacy_config.parent.mkdir(parents=True)
    current_config.parent.mkdir(parents=True)
    legacy_config.write_text("value = 'legacy'\n", encoding="utf-8")
    current_config.write_text("value = 'current'\n", encoding="utf-8")

    report = migrate_legacy_state(user_home=home, workspace=workspace)

    assert current_config.read_text(encoding="utf-8") == "value = 'current'\n"
    assert report.user_files_copied == 0


# 功能：验证项目状态会迁移但受管 worktree 不会被移动或复制
# 设计：同时创建 memory 与 worktrees，断言只迁移可移植状态，避免破坏 Git worktree 元数据
def test_project_migration_skips_managed_worktrees(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    memory = workspace / ".kyle" / "memory" / "records" / "mem-1.json"
    worktree = workspace / ".kyle" / "worktrees" / "worker-1" / "file.py"
    memory.parent.mkdir(parents=True)
    worktree.parent.mkdir(parents=True)
    memory.write_text(json.dumps({"body": "project rule"}), encoding="utf-8")
    worktree.write_text("print('old worktree')\n", encoding="utf-8")

    report = migrate_legacy_state(
        user_home=home,
        workspace=workspace,
        include_project=True,
    )

    assert (workspace / ".coderook" / "memory" / "records" / "mem-1.json").is_file()
    assert not (workspace / ".coderook" / "worktrees").exists()
    assert report.project_files_copied == 1


# 功能：验证普通 CLI/TUI/Core 启动不会自动复制工作区内的旧项目状态
# 设计：保留真实旧 memory 文件并使用默认参数，断言只报告发现而不创建项目 .coderook
def test_project_migration_requires_explicit_confirmation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    legacy = workspace / ".kyle" / "memory" / "rule.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy project state", encoding="utf-8")

    report = migrate_legacy_state(user_home=home, workspace=workspace)

    assert report.legacy_project_state_found is True
    assert report.project_files_copied == 0
    assert not (workspace / ".coderook").exists()


# 功能：验证显式项目迁移拒绝把 .coderook 根 symlink 当作写入目录
# 设计：让目标指向工作区外哨兵目录，若平台支持 symlink 则断言外部文件始终不会出现
def test_project_migration_rejects_symlinked_target_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    legacy = workspace / ".kyle" / "memory" / "rule.md"
    legacy.parent.mkdir(parents=True)
    outside.mkdir()
    legacy.write_text("must stay inside", encoding="utf-8")
    try:
        os.symlink(outside, workspace / ".coderook", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(RuntimeError, match="must not be a symlink"):
        migrate_legacy_state(
            user_home=home,
            workspace=workspace,
            include_project=True,
        )

    assert not (outside / "memory" / "rule.md").exists()


# 功能：验证迁移拒绝穿过 .coderook 内部已有的目录 symlink
# 设计：只把 memory 子目录指向外部，覆盖根目录合法但递归目标逃逸的路径
def test_project_migration_rejects_nested_target_symlink(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    legacy = workspace / ".kyle" / "memory" / "rule.md"
    target_root = workspace / ".coderook"
    legacy.parent.mkdir(parents=True)
    target_root.mkdir(parents=True)
    outside.mkdir()
    legacy.write_text("must stay inside", encoding="utf-8")
    try:
        os.symlink(outside, target_root / "memory", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(RuntimeError, match="symlink"):
        migrate_legacy_state(
            user_home=home,
            workspace=workspace,
            include_project=True,
        )

    assert not (outside / "rule.md").exists()


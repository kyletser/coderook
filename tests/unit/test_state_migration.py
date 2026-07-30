from __future__ import annotations

import json
from pathlib import Path

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

    report = migrate_legacy_state(user_home=home, workspace=workspace)

    assert (workspace / ".coderook" / "memory" / "records" / "mem-1.json").is_file()
    assert not (workspace / ".coderook" / "worktrees").exists()
    assert report.project_files_copied == 1


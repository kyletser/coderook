from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_rook.core.worktree import WorktreeError, WorktreeManager


# 初始化包含一次提交的最小 Git 仓库
def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=CodeRook Test",
            "-c",
            "user.email=coderook@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


# 功能：验证 worktree 可以在固定目录创建、列出并安全删除
# 设计：使用真实临时 Git 仓库覆盖完整生命周期，避免用 mock 掩盖 Git 参数错误
async def test_worktree_lifecycle(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)

    path = await manager.create("review")
    listed = await manager.list()
    await manager.remove("review")

    assert path.name == "review"
    assert listed[0]["name"] == "review"
    assert not path.exists()


# 功能：验证脏 worktree 默认不能被删除，显式 discard 后才允许清理
# 设计：创建未跟踪文件模拟并行 agent 修改，先断言保护错误，再执行强制清理
async def test_worktree_remove_protects_dirty_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    path = await manager.create("dirty")
    (path / "change.txt").write_text("work\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="uncommitted"):
        await manager.remove("dirty")

    await manager.remove("dirty", discard_changes=True)
    assert not path.exists()


# 功能：验证非法 worktree 名称在执行 Git 前被拒绝
# 设计：传入父目录穿越字符串，断言领域错误以覆盖固定目录安全边界
def test_worktree_name_rejects_traversal(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    with pytest.raises(WorktreeError, match="invalid"):
        manager.path_for("../escape")

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


# 功能：worktree 检查相对固定基线返回真实路径、统计和有界 diff
# 设计：同时制造 tracked 与 untracked 变化，验证状态清单不会依赖 Worker 自报结果
async def test_worktree_inspection_collects_authoritative_changes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    base_commit = await manager.resolve_ref()
    path = await manager.create("inspect", base_commit)
    (path / "README.md").write_text("changed\n", encoding="utf-8")
    (path / "new.txt").write_text("new\n", encoding="utf-8")

    result = await manager.inspect("inspect", base_commit=base_commit)

    assert result.base_commit == base_commit
    assert result.changed_files == ("README.md", "new.txt")
    assert "README.md" in result.diff_stat
    assert "+changed" in result.diff


# 功能：经摘要审查的 Worker 补丁可完整应用到干净主工作区且保持为未暂存改动
# 设计：同时修改 tracked 文件并新增 untracked 文件，覆盖临时 index、三方预检和最终路径对账
async def test_reviewed_worktree_apply_preserves_complete_unstaged_patch(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    base_commit = await manager.resolve_ref()
    path = await manager.create("apply", base_commit)
    (path / "README.md").write_text("reviewed\n", encoding="utf-8")
    (path / "new.txt").write_text("new\n", encoding="utf-8")
    preview = await manager.preview_apply("apply", base_commit=base_commit)

    assert preview.diff_truncated is False
    assert "diff --git a/new.txt b/new.txt" in preview.diff
    assert "+new" in preview.diff

    result = await manager.apply(
        "apply",
        base_commit=base_commit,
        expected_digest=preview.state_digest,
        reviewed_files=preview.changed_files,
    )

    assert result.changed_files == ("README.md", "new.txt")
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "reviewed\n"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new\n"
    staged = subprocess.run(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert staged.stdout == ""


# 功能：Worker 文件在审查后变化时旧摘要必须失效且主工作区保持干净
# 设计：先生成预览再二次修改同一文件，断言 TOCTOU 重验早于任何主仓库写入
async def test_worktree_apply_rejects_stale_review_digest(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    base_commit = await manager.resolve_ref()
    path = await manager.create("stale", base_commit)
    (path / "README.md").write_text("reviewed\n", encoding="utf-8")
    preview = await manager.preview_apply("stale", base_commit=base_commit)
    (path / "README.md").write_text("changed-after-review\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="stale"):
        await manager.apply(
            "stale",
            base_commit=base_commit,
            expected_digest=preview.state_digest,
            reviewed_files=preview.changed_files,
        )

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "base\n"
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


# 功能：主仓库有任何本地改动时禁止应用 Worker，避免覆盖用户正在编辑的文件
# 设计：在预览前制造独立未跟踪文件，验证干净工作区门禁对 tracked/untracked 一视同仁
async def test_worktree_apply_requires_clean_main_workspace(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    base_commit = await manager.resolve_ref()
    path = await manager.create("dirty-main", base_commit)
    (path / "README.md").write_text("reviewed\n", encoding="utf-8")
    (tmp_path / "local.txt").write_text("user work\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="clean"):
        await manager.preview_apply("dirty-main", base_commit=base_commit)

    assert (tmp_path / "local.txt").read_text(encoding="utf-8") == "user work\n"


# 功能：Worker 补丁临时索引子进程不会继承 API Key 等 daemon 敏感环境变量
# 设计：拦截两次带环境的 Git 调用，直接检查安全白名单和必要 index 控制变量
async def test_worktree_patch_generation_sanitizes_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = WorktreeManager(tmp_path)
    captured: list[dict[str, str]] = []
    monkeypatch.setenv("CODEROOK_TEST_API_KEY", "must-not-leak")

    # 捕获临时 index 准备调用而不执行 Git
    async def fake_git_env(env: dict[str, str], *_args: str) -> str:
        captured.append(dict(env))
        return ""

    # 捕获补丁生成调用并返回最小非空补丁占位
    async def fake_git_bytes_env(
        env: dict[str, str] | None,
        *_args: str,
    ) -> bytes:
        assert env is not None
        captured.append(dict(env))
        return b"patch"

    monkeypatch.setattr(manager, "_git_env", fake_git_env)
    monkeypatch.setattr(manager, "_git_bytes_env", fake_git_bytes_env)

    patch = await manager._build_complete_patch(tmp_path, base_commit="a" * 40)

    assert patch == b"patch"
    assert captured
    assert all("CODEROOK_TEST_API_KEY" not in env for env in captured)
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" for env in captured)
    assert all("GIT_INDEX_FILE" in env for env in captured)


# 功能：验证非法 worktree 名称在执行 Git 前被拒绝
# 设计：传入父目录穿越字符串，断言领域错误以覆盖固定目录安全边界
def test_worktree_name_rejects_traversal(tmp_path: Path) -> None:
    manager = WorktreeManager(tmp_path)
    with pytest.raises(WorktreeError, match="invalid"):
        manager.path_for("../escape")

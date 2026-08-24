from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_rook.core.state_paths import (
    StatePathSecurityError,
    prepare_user_state_layout,
    secure_user_state_root,
)


# 功能：验证正常用户状态根会建立受控 Session/Goal 目录但不会抢先创建数据库
# 设计：从空临时目录执行一次布局准备，核对全部路径身份与 Runtime 延迟创建语义
def test_prepare_user_state_layout_creates_safe_directories(tmp_path: Path) -> None:
    root = tmp_path / ".coderook"

    layout = prepare_user_state_layout(root)

    assert layout.root == root.absolute()
    assert layout.sessions.is_dir()
    assert layout.goals.is_dir()
    assert not layout.runtime_database.exists()


# 功能：验证用户状态根本身为 symlink 时所有升级和 daemon 写入都会失败关闭
# 设计：把状态根链接到外部目录，断言安全根校验不跟随且外部目录保持为空
def test_secure_user_state_root_refuses_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / ".coderook"
    try:
        os.symlink(outside, root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(StatePathSecurityError, match="real directory"):
        secure_user_state_root(root, create=True)

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["sessions", "goals", "backups", "migrations"])
# 功能：验证关键状态子目录为 symlink 时布局准备不会在外部创建或隔离文件
# 设计：分别链接 Session 与 Goal 目录到外部哨兵，覆盖 daemon 两个文件账本入口
def test_prepare_user_state_layout_refuses_symlinked_directory(
    tmp_path: Path,
    name: str,
) -> None:
    root = tmp_path / ".coderook"
    outside = tmp_path / f"outside-{name}"
    root.mkdir()
    outside.mkdir()
    (outside / "sentinel").write_text("keep", encoding="utf-8")
    try:
        os.symlink(outside, root / name, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(StatePathSecurityError, match=name):
        prepare_user_state_layout(root)

    assert (outside / "sentinel").read_text(encoding="utf-8") == "keep"


# 功能：验证 Runtime 数据库文件 symlink 不能把迁移或恢复写入状态根之外
# 设计：链接外部普通文件到固定数据库名，断言布局准备在任何 SQLite 打开前拒绝
def test_prepare_user_state_layout_refuses_symlinked_runtime(tmp_path: Path) -> None:
    root = tmp_path / ".coderook"
    root.mkdir()
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"sentinel")
    try:
        os.symlink(outside, root / "runtime.db")
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    with pytest.raises(StatePathSecurityError, match="runtime database"):
        prepare_user_state_layout(root)

    assert outside.read_bytes() == b"sentinel"

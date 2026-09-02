from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.api.web_auth import WebAuthManager
from code_rook.core.projects import ProjectHandoffTickets, ProjectRegistry


# 功能：验证空白项目创建到默认目录且能持久出现在最近项目列表
# 设计：隔离状态根和默认项目根，避免访问真实用户目录并同时覆盖创建、注册与重载
def test_create_blank_project_uses_default_location(tmp_path: Path) -> None:
    registry = ProjectRegistry(
        state_root=tmp_path / "state",
        default_projects_root=tmp_path / "projects",
    )

    created = registry.create_blank("demo-agent")
    reloaded = ProjectRegistry(
        state_root=tmp_path / "state",
        default_projects_root=tmp_path / "projects",
    ).list_projects()

    assert Path(created.root) == (tmp_path / "projects" / "demo-agent").resolve()
    assert created.kind == "blank"
    assert reloaded == [created]


# 功能：验证已有项目只登记目录而不会复制、移动或改写其中的文件
# 设计：在目录放入哨兵文件后执行注册，断言路径保持原位且内容完全不变
def test_register_existing_project_keeps_user_files(tmp_path: Path) -> None:
    workspace = tmp_path / "existing"
    workspace.mkdir()
    marker = workspace / "README.md"
    marker.write_text("owned by user", encoding="utf-8")
    registry = ProjectRegistry(state_root=tmp_path / "state")

    record = registry.register(workspace)

    assert record.root == str(workspace.resolve())
    assert record.kind == "existing"
    assert marker.read_text(encoding="utf-8") == "owned by user"


@pytest.mark.parametrize("name", ["../escape", "nested/name", "CON", "bad:name"])
# 功能：验证无效项目名和重复目录不会覆盖磁盘上的已有项目
# 设计：参数化路径分隔符与 Windows 保留名，并用已存在目录覆盖冲突分支
def test_create_blank_project_rejects_unsafe_names(tmp_path: Path, name: str) -> None:
    registry = ProjectRegistry(
        state_root=tmp_path / "state",
        default_projects_root=tmp_path / "projects",
    )

    with pytest.raises(ValueError):
        registry.create_blank(name)

    existing = tmp_path / "projects" / "demo"
    existing.mkdir(parents=True, exist_ok=True)
    with pytest.raises(FileExistsError):
        registry.create_blank("demo")


# 功能：验证跨 Core 项目切换票据仅能在目标工作区交换一次
# 设计：用共享临时票据目录构造旧新两个认证管理器，覆盖工作区绑定与单次消费
def test_project_handoff_ticket_is_workspace_bound_and_single_use(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    tickets = ProjectHandoffTickets(tmp_path / "state")
    token = tickets.issue(second)
    wrong = WebAuthManager(workspace=first, handoff_tickets=tickets)
    target = WebAuthManager(workspace=second, handoff_tickets=tickets)

    assert wrong.exchange(token) is None
    assert target.exchange(token) is not None
    assert target.exchange(token) is None

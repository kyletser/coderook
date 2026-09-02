from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core import projects as projects_module
from code_rook.core.projects import ProjectRegistry


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


# 功能：验证运行中的 CodeRook 源码目录不能作为用户项目注册或重新出现在最近列表
# 设计：用临时源码根替代运行时探测，并预写一条旧记录覆盖注册拒绝与历史过滤两个入口
def test_source_checkout_is_hidden_from_user_projects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "coderook-source"
    source.mkdir()
    registry = ProjectRegistry(state_root=tmp_path / "state")
    monkeypatch.setattr(projects_module, "_running_source_checkout", lambda: source)

    with pytest.raises(ValueError, match="internal source"):
        registry.register(source)

    user_project = tmp_path / "user-project"
    user_project.mkdir()
    registered = registry.register(user_project)
    stale = registered.__class__(
        id="source",
        name="coderook",
        root=str(source),
        kind="existing",
        created_at=0,
        last_opened_at=999,
    )
    registry._save([registered, stale])

    assert registry.list_projects() == [registered]


# 功能：验证未选择项目的欢迎工作区不会被登记为普通项目
# 设计：真实创建隔离欢迎目录后走公开注册入口，确保后端边界不依赖前端隐藏按钮
def test_welcome_workspace_cannot_be_registered(tmp_path: Path) -> None:
    registry = ProjectRegistry(state_root=tmp_path / "state")
    welcome = registry.prepare_welcome_workspace()

    assert registry.is_protected_workspace(welcome) is True
    with pytest.raises(ValueError, match="welcome workspace"):
        registry.register(welcome)


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

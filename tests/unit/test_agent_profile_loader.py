from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.agents.loader import AgentProfileLoader


# 功能：内建 planner 角色配置应能被 AgentProfileLoader 加载
# 设计：直接调用 load("planner")，验证关键字段非空
def test_builtin_planner_found() -> None:
    loader = AgentProfileLoader()
    profile = loader.load("planner")
    assert profile is not None
    assert profile.name == "planner"
    assert profile.system_prompt != ""
    assert "read_file" in profile.allowed_tools or len(profile.allowed_tools) > 0


# 功能：内建三种角色均可加载
# 设计：参数化测试所有内建角色名；reviewer 走 restrict 而非 allowed_tools
@pytest.mark.parametrize("role", ["planner", "executor", "reviewer"])
def test_all_builtin_roles_found(role: str) -> None:
    loader = AgentProfileLoader()
    profile = loader.load(role)
    assert profile is not None, f"builtin role '{role}' not found"
    if role == "reviewer":
        # reviewer 以 capability restrict 过滤工具集，故 allowed_tools 可为空
        assert profile.restrict == "read_only"
        return
    assert profile.allowed_tools  # planner / executor 必须列 allowed_tools
    assert "glob" in profile.allowed_tools
    assert "grep" in profile.allowed_tools
    assert "git_diff" in profile.allowed_tools
    assert "checkpoint_list" in profile.allowed_tools
    if role == "executor":
        assert "edit_file" in profile.allowed_tools
        assert "apply_patch" in profile.allowed_tools
        assert "checkpoint_rewind" in profile.allowed_tools


# 功能：restrict 字段应能被 TOML 解析为 AgentProfile.restrict
# 设计：写入 restrict = "read_only" 的临时 TOML，断言 profile.restrict 等于该值
def test_restrict_field_parsed(tmp_path: Path) -> None:
    content = """\
[agent]
description = "只读角色"
system_prompt = "只允许检查。"
allowed_tools = []
restrict = "read_only"
model = ""
"""
    p = tmp_path / "auditor.toml"
    p.write_text(content, encoding="utf-8")
    loader = AgentProfileLoader()
    profile = loader._parse(p, "auditor")
    assert profile is not None
    assert profile.restrict == "read_only"


# 功能：未知角色名应返回 None
# 设计：查找不存在的角色，断言返回 None 而非抛异常
def test_unknown_role_returns_none() -> None:
    loader = AgentProfileLoader()
    result = loader.load("nonexistent_role_xyz")
    assert result is None


# 功能：TOML 角色配置文件应被正确解析
# 设计：写入临时 TOML 文件，通过 _parse 解析并验证所有字段
def test_toml_parsed(tmp_path: Path) -> None:
    content = """\
[agent]
description = "测试角色"
system_prompt = "你是测试助手。"
allowed_tools = ["read_file", "bash"]
model = "claude-sonnet-4-6"
"""
    p = tmp_path / "tester.toml"
    p.write_text(content, encoding="utf-8")
    loader = AgentProfileLoader()
    profile = loader._parse(p, "tester")
    assert profile.name == "tester"
    assert profile.description == "测试角色"
    assert profile.system_prompt == "你是测试助手。"
    assert "read_file" in profile.allowed_tools
    assert "bash" in profile.allowed_tools
    assert profile.model == "claude-sonnet-4-6"


# 功能：项目本地角色配置应覆盖内建同名配置
# 设计：在 .coderook/agents/ 中写入同名 TOML，monkeypatch cwd，断言加载到本地版本
def test_project_overrides_builtin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_agents = tmp_path / ".coderook" / "agents"
    local_agents.mkdir(parents=True)
    (local_agents / "planner.toml").write_text(
        '[agent]\ndescription = "local planner"\nsystem_prompt = "local prompt"\n'
        'allowed_tools = ["list_dir"]\nmodel = ""\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = AgentProfileLoader()
    profile = loader.load("planner")
    assert profile is not None
    assert profile.description == "local planner"
    assert "list_dir" in profile.allowed_tools

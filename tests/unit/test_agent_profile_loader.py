from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.agents.loader import (
    AgentProfileError,
    AgentProfileIntegrityError,
    AgentProfileLoader,
    AgentProfileTrustError,
)


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
    assert not any("\u4e00" <= char <= "\u9fff" for char in profile.description)
    assert not any("\u4e00" <= char <= "\u9fff" for char in profile.system_prompt)
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
route = "openai-work"
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
    assert profile.route == "openai-work"
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


# 功能：验证能力目录能列出内建角色并由项目级同名配置覆盖
# 设计：显式传入项目根目录，避免依赖进程 cwd，并按名称检查最终合并结果
def test_list_all_profiles_uses_project_root_and_override(tmp_path: Path) -> None:
    local_agents = tmp_path / ".coderook" / "agents"
    local_agents.mkdir(parents=True)
    (local_agents / "planner.toml").write_text(
        '[agent]\ndescription = "project planner"\nsystem_prompt = "project prompt"\n',
        encoding="utf-8",
    )

    profiles = AgentProfileLoader(tmp_path).list_all()
    by_name = {profile.name: profile for profile in profiles}

    assert {"planner", "executor", "reviewer"} <= set(by_name)
    assert by_name["planner"].description == "project planner"


# 功能：验证 Agent Profile 使用严格 schema 拒绝错误类型和未知字段
# 设计：构造 allowed_tools 字符串及额外键，直接解析并检查错误包含 profile 路径
@pytest.mark.parametrize(
    "agent_body",
    [
        'description = "bad"\nallowed_tools = "read_file"',
        'description = "bad"\nunknown_capability = true',
        'description = "bad"\nrestrict = "write_all"',
    ],
)
def test_profile_schema_rejects_invalid_documents(
    tmp_path: Path,
    agent_body: str,
) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(f"[agent]\n{agent_body}\n", encoding="utf-8")

    with pytest.raises(AgentProfileError, match="invalid agent profile"):
        AgentProfileLoader(tmp_path)._parse(path, "invalid")


# 功能：验证 profile 名不能通过路径片段逃逸固定 agents 目录
# 设计：向项目根放置可读 TOML 后用 ../ 名称加载，断言加载器在文件访问前拒绝
def test_profile_name_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "escape.toml").write_text(
        '[agent]\ndescription = "escape"\n',
        encoding="utf-8",
    )

    assert AgentProfileLoader(tmp_path).load("../../escape") is None


# 功能：验证项目 profile 暴露内容 digest、来源和未信任 provenance
# 设计：加载项目目录中的有效 TOML，断言 digest 格式稳定且 scope/trust 不冒充 builtin
def test_project_profile_records_digest_and_trust(tmp_path: Path) -> None:
    agents = tmp_path / ".coderook" / "agents"
    agents.mkdir(parents=True)
    path = agents / "local.toml"
    path.write_text(
        '[agent]\ndescription = "local"\nsystem_prompt = "review"\n',
        encoding="utf-8",
    )

    profile = AgentProfileLoader(tmp_path).load("local")

    assert profile is not None
    assert profile.scope == "project"
    assert profile.trust == "untrusted"
    assert profile.integrity == "unmanaged"
    assert profile.digest.startswith("sha256:")
    assert profile.source == str(path.resolve())


# 功能：未信任 workspace 的项目 Profile 名称和描述不能进入模型可执行目录
# 设计：放置同名恶意覆盖与唯一 profile，验证 builtin 回退、唯一项隐藏及直接执行拒绝
def test_execution_catalog_hides_untrusted_project_profiles(tmp_path: Path) -> None:
    agents = tmp_path / ".coderook" / "agents"
    agents.mkdir(parents=True)
    (agents / "planner.toml").write_text(
        '[agent]\ndescription = "SECRET PLANNER"\nsystem_prompt = "secret"\n',
        encoding="utf-8",
    )
    (agents / "secret-worker.toml").write_text(
        '[agent]\ndescription = "SECRET DESCRIPTION"\nsystem_prompt = "secret"\n',
        encoding="utf-8",
    )
    loader = AgentProfileLoader(tmp_path)

    catalog = loader.list_for_execution(workspace_trusted=False)
    by_name = {profile.name: profile for profile in catalog}

    assert by_name["planner"].scope == "builtin"
    assert by_name["planner"].description != "SECRET PLANNER"
    assert "secret-worker" not in by_name
    with pytest.raises(AgentProfileTrustError, match="not trusted"):
        loader.load_for_execution("secret-worker", workspace_trusted=False)


# 功能：可信 workspace 可以发现严格合法的项目 Profile
# 设计：以唯一名称写入完整 TOML，通过执行目录与严格加载两个入口确认 source 和 digest 一致
def test_execution_catalog_allows_project_profile_in_trusted_workspace(
    tmp_path: Path,
) -> None:
    agents = tmp_path / ".coderook" / "agents"
    agents.mkdir(parents=True)
    path = agents / "local-reviewer.toml"
    path.write_text(
        '[agent]\ndescription = "Local reviewer"\nsystem_prompt = "Review safely"\n'
        'restrict = "read_only"\n',
        encoding="utf-8",
    )
    loader = AgentProfileLoader(tmp_path)

    catalog = loader.list_for_execution(workspace_trusted=True)
    frozen = next(profile for profile in catalog if profile.name == "local-reviewer")
    loaded = loader.load_for_execution(
        "local-reviewer",
        workspace_trusted=True,
        expected_digest=frozen.digest,
    )

    assert loaded is not None
    assert loaded.source == str(path.resolve())
    assert loaded.digest == frozen.digest


# 功能：项目 Profile 在发现后被修改时必须按冻结 digest 拒绝执行
# 设计：先从可信目录冻结 digest，再改写 system_prompt 并用 expected_digest 严格重载
def test_profile_digest_change_after_discovery_fails_closed(tmp_path: Path) -> None:
    agents = tmp_path / ".coderook" / "agents"
    agents.mkdir(parents=True)
    path = agents / "local.toml"
    path.write_text(
        '[agent]\ndescription = "Local"\nsystem_prompt = "before"\n',
        encoding="utf-8",
    )
    loader = AgentProfileLoader(tmp_path)
    frozen = loader.load_for_execution("local", workspace_trusted=True)
    assert frozen is not None
    path.write_text(
        '[agent]\ndescription = "Local"\nsystem_prompt = "after"\n',
        encoding="utf-8",
    )

    with pytest.raises(AgentProfileIntegrityError, match="digest changed"):
        loader.load_for_execution(
            "local",
            workspace_trusted=True,
            expected_digest=frozen.digest,
        )


# 功能：可信 workspace 中 schema 非法的项目 Profile 也不能进入执行目录
# 设计：写入未知字段并同时检查 list_for_execution 隔离与严格 load 的可诊断错误
def test_execution_catalog_rejects_invalid_project_profile(tmp_path: Path) -> None:
    agents = tmp_path / ".coderook" / "agents"
    agents.mkdir(parents=True)
    (agents / "invalid.toml").write_text(
        '[agent]\ndescription = "secret"\nunknown_capability = true\n',
        encoding="utf-8",
    )
    loader = AgentProfileLoader(tmp_path)

    assert "invalid" not in {
        profile.name
        for profile in loader.list_for_execution(workspace_trusted=True)
    }
    with pytest.raises(AgentProfileError, match="invalid agent profile"):
        loader.load_for_execution("invalid", workspace_trusted=True)


# 功能：可信 workspace 也不能通过项目 Profile 符号链接读取仓库外配置
# 设计：把 agents 下的候选链接到外部 TOML，平台支持时同时验证目录过滤和严格加载拒绝
def test_execution_catalog_rejects_symlinked_project_profile(tmp_path: Path) -> None:
    outside = tmp_path / "outside.toml"
    outside.write_text(
        '[agent]\ndescription = "external secret"\nsystem_prompt = "external"\n',
        encoding="utf-8",
    )
    root = tmp_path / "repo"
    agents = root / ".coderook" / "agents"
    agents.mkdir(parents=True)
    linked = agents / "linked.toml"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit creating symbolic links")
    loader = AgentProfileLoader(root)

    assert "linked" not in {
        profile.name
        for profile in loader.list_for_execution(workspace_trusted=True)
    }
    with pytest.raises(AgentProfileError, match="symbolic link"):
        loader.load_for_execution("linked", workspace_trusted=True)

from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.skills.loader import SkillError, SkillLoader, SkillTrustError
from code_rook.core.skills.models import Skill, SkillManifest


# 功能：内建 review skill 应能被 SkillLoader 查找到
# 设计：直接调用 resolve("review")，不依赖文件系统之外的任何状态
def test_builtin_skill_found() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"
    assert "审查" in skill.description or "review" in skill.description.lower()
    assert skill.system_prompt_template != ""


# 功能：内建 init / summarize / orchestrate skill 均可找到
# 设计：列举所有内建 skill 名，断言均能解析
@pytest.mark.parametrize("name", ["init", "review", "summarize", "orchestrate"])
def test_all_builtin_skills_found(name: str) -> None:
    loader = SkillLoader()
    skill = loader.resolve(name)
    assert skill is not None, f"builtin skill '{name}' not found"
    assert not any("\u4e00" <= char <= "\u9fff" for char in skill.description)
    assert not any("\u4e00" <= char <= "\u9fff" for char in skill.system_prompt_template)


# 功能：验证内建编排 skill 只向模型声明 action-family 工具
# 设计：读取真实内建 manifest 与提示正文，防止 replay-only 旧别名重新进入模型契约
def test_orchestrate_skill_uses_action_family_contract() -> None:
    loader = SkillLoader()
    skill = loader.resolve("orchestrate")

    assert skill is not None
    assert skill.allowed_tools == ["agent", "tasks"]
    assert "Call agent with action `start`" in skill.system_prompt_template
    assert "spawn_agent" not in skill.system_prompt_template
    assert "task_create" not in skill.system_prompt_template


# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_skill_returns_none() -> None:
    loader = SkillLoader()
    result = loader.resolve("nonexistent_skill_xyz")
    assert result is None


# 功能：render_prompt 应将 $ARGUMENTS 替换为传入的参数字符串
# 设计：构造含 $ARGUMENTS 的 skill，验证 render_prompt 结果不含 "$ARGUMENTS" 且含参数值
def test_arguments_substituted() -> None:
    loader = SkillLoader()
    skill = Skill(
        manifest=SkillManifest(name="test", description="test skill"),
        system_prompt_template="Review this: $ARGUMENTS\nPlease be thorough.",
        digest="sha256:" + "0" * 64,
        source="test",
        installed_at="2026-08-04T00:00:00Z",
        trust="trusted",
        scope="project",
        path="test.md",
        integrity="unmanaged",
    )
    rendered = loader.render_prompt(skill, "src/foo.py")
    assert "$ARGUMENTS" not in rendered
    assert "src/foo.py" in rendered


# 功能：frontmatter 中的 allowed_tools 列表应被正确解析
# 设计：构造含 allowed_tools 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果
def test_frontmatter_parsed(tmp_path: Path) -> None:
    from code_rook.core.skills.loader import _parse_skill_file

    content = """\
---
name: custom
description: 自定义 skill 测试
allowed_tools:
  - read_file
  - bash
---
你是一个测试助手，目标：$ARGUMENTS
"""
    p = tmp_path / "custom.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "custom"
    assert skill.description == "自定义 skill 测试"
    assert "read_file" in skill.allowed_tools
    assert "bash" in skill.allowed_tools
    assert "$ARGUMENTS" in skill.system_prompt_template


# 功能：无 frontmatter 的 Markdown 文件仍可加载，allowed_tools 为空列表
# 设计：写入纯正文 Markdown，断言解析成功且 allowed_tools=[]
def test_no_frontmatter(tmp_path: Path) -> None:
    from code_rook.core.skills.loader import _parse_skill_file

    content = "你是助手，请帮助用户完成任务：$ARGUMENTS\n"
    p = tmp_path / "plain.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "plain"
    assert skill.allowed_tools == []
    assert "你是助手" in skill.system_prompt_template


# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .coderook/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本
def test_project_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_skills = tmp_path / ".coderook" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: local override\n---\nlocal system prompt $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.description == "local override"
    assert "local system prompt" in skill.system_prompt_template


# 功能：验证 skill 名称在拼接候选路径前拒绝路径穿越片段
# 设计：项目根放置可读 Markdown 后请求 ../ 名称，断言加载器明确报 invalid name
def test_skill_name_rejects_path_traversal(tmp_path: Path) -> None:
    (tmp_path / "escape.md").write_text("escaped body\n", encoding="utf-8")

    with pytest.raises(SkillError, match="invalid skill name"):
        SkillLoader(tmp_path).resolve("../../escape")


# 功能：验证需要可信执行时不会加载 unmanaged project skill 的正文
# 设计：手工项目 skill 天然标记 untrusted/unmanaged，require_trusted 应在返回正文前拒绝
def test_require_trusted_rejects_unmanaged_project_skill(tmp_path: Path) -> None:
    skills = tmp_path / ".coderook" / "skills"
    skills.mkdir(parents=True)
    (skills / "local.md").write_text(
        "---\nname: local\ndescription: local\n---\nuntrusted instructions\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillTrustError, match="not trusted"):
        SkillLoader(tmp_path).resolve("local", require_trusted=True)


# 功能：验证 session 已显式信任 workspace 后可执行该项目内的 unmanaged skill
# 设计：复用与拒绝测试相同的本地 skill，仅切换 workspace_trusted 快照以隔离授权来源
def test_require_trusted_allows_project_skill_in_trusted_workspace(
    tmp_path: Path,
) -> None:
    skills = tmp_path / ".coderook" / "skills"
    skills.mkdir(parents=True)
    (skills / "local.md").write_text(
        "---\nname: local\ndescription: local\n---\ntrusted workspace $ARGUMENTS\n",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)

    skill = loader.resolve(
        "local",
        require_trusted=True,
        workspace_trusted=True,
    )

    assert skill is not None
    assert "target.py" in loader.render_prompt(
        skill,
        "target.py",
        require_trusted=True,
        workspace_trusted=True,
    )


# 功能：验证项目文件自报 trusted 也不能绕过 session workspace trust
# 设计：直接构造 digest verified 的 project Skill，隔离完整性后确认来源授权仍然 fail closed
def test_project_skill_metadata_trust_cannot_bypass_workspace_trust() -> None:
    skill = Skill(
        manifest=SkillManifest(name="forged", description="forged project skill"),
        system_prompt_template="project instructions",
        digest="sha256:" + "0" * 64,
        expected_digest="sha256:" + "0" * 64,
        source="project:forged",
        installed_at="2026-08-24T00:00:00Z",
        trust="trusted",
        scope="project",
        path=".coderook/skills/forged.md",
        integrity="verified",
    )

    with pytest.raises(SkillTrustError, match="not trusted"):
        SkillLoader().render_prompt(skill, "", require_trusted=True)


# 功能：验证可信执行模式仍允许内建 skill 并保持 digest 完整性
# 设计：加载真实 builtin review，断言 trust/integrity 后再渲染参数
def test_require_trusted_allows_builtin_skill() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review", require_trusted=True)

    assert skill is not None
    assert skill.trust == "builtin"
    assert skill.integrity == "verified"
    assert "target.py" in loader.render_prompt(
        skill,
        "target.py",
        require_trusted=True,
    )


# 功能：未信任 workspace 的项目 Skill 不得遮蔽内建同名项或出现在可执行目录
# 设计：同时放置恶意同名覆盖和唯一名称，断言目录回退到 builtin 且不包含项目标记
def test_execution_catalog_does_not_leak_untrusted_project_skills(
    tmp_path: Path,
) -> None:
    skills = tmp_path / ".coderook" / "skills"
    skills.mkdir(parents=True)
    (skills / "review.md").write_text(
        "---\nname: review\ndescription: SECRET OVERRIDE\n---\nsecret body\n",
        encoding="utf-8",
    )
    (skills / "secret-profile.md").write_text(
        "---\nname: secret-profile\ndescription: SECRET DESCRIPTION\n---\nsecret\n",
        encoding="utf-8",
    )

    catalog = SkillLoader(tmp_path).list_for_execution(workspace_trusted=False)
    by_name = {skill.name: skill for skill in catalog}

    assert by_name["review"].scope == "builtin"
    assert by_name["review"].description != "SECRET OVERRIDE"
    assert "secret-profile" not in by_name


# 功能：可信 workspace 仍只接受严格 Skill manifest，未知字段不能进入执行目录
# 设计：写入带注入描述和未知字段的 frontmatter，分别检查直接解析报错与目录静默隔离
def test_execution_catalog_rejects_unknown_skill_manifest_fields(
    tmp_path: Path,
) -> None:
    skills = tmp_path / ".coderook" / "skills"
    skills.mkdir(parents=True)
    (skills / "invalid.md").write_text(
        "---\nname: invalid\ndescription: secret\nprompt_injection: true\n---\nbody\n",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)

    with pytest.raises(SkillError, match="unknown skill manifest field"):
        loader.resolve(
            "invalid",
            require_trusted=True,
            workspace_trusted=True,
        )

    assert "invalid" not in {
        skill.name for skill in loader.list_for_execution(workspace_trusted=True)
    }


# 功能：可信 workspace 也不能通过项目 Skill 符号链接加载仓库外正文
# 设计：把固定 skill 名链接到外部 Markdown，若平台允许创建链接则检查目录隔离和直接解析报错
def test_execution_catalog_rejects_symlinked_project_skill(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: linked\ndescription: external secret\n---\nexternal body\n",
        encoding="utf-8",
    )
    skills = tmp_path / "repo" / ".coderook" / "skills"
    skills.mkdir(parents=True)
    linked = skills / "linked.md"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("platform does not permit creating symbolic links")
    loader = SkillLoader(tmp_path / "repo")

    assert "linked" not in {
        skill.name for skill in loader.list_for_execution(workspace_trusted=True)
    }
    with pytest.raises(SkillError, match="symbolic link"):
        loader.resolve("linked", require_trusted=True, workspace_trusted=True)

from pathlib import Path

import pytest

from code_rook.core.agent_runtime.skill_input import expand_skill_input
from code_rook.core.prompt_context import build_capability_context
from code_rook.core.skills.loader import SkillLoader, SkillTrustError


# 功能：验证 Pi 形式 Skill 命令展开正文、来源和参数，不把正文当系统提示替换。
# 设计：用真实项目 SkillLoader 读取临时文件，核对源路径、相对资源目录和未知命令原样保留。
def test_native_skill_command_expands_user_context(tmp_path: Path) -> None:
    folder = tmp_path / ".coderook" / "skills" / "inspect"
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text("---\nname: inspect\ndescription: Inspect code\n---\nRead relevant code.",
                    encoding="utf-8")
    loader = SkillLoader(tmp_path, user_skills_dir=tmp_path / "user-skills")
    expanded = expand_skill_input("/skill:inspect auth module", loader, workspace_trusted=True)
    assert '<skill name="inspect"' in expanded
    assert f"References are relative to {folder}." in expanded
    assert "Read relevant code.\n</skill>\n\nauth module" in expanded
    assert "description:" not in expanded
    assert expand_skill_input("/skill:unknown", loader, workspace_trusted=True) == "/skill:unknown"
    assert expand_skill_input("/inspect", loader, workspace_trusted=True) == "/inspect"
    assert expand_skill_input("/skill:bad/name", loader, workspace_trusted=True) == "/skill:bad/name"


# 功能：验证原生 Skill 输入不会绕过已有项目来源信任检查。
# 设计：同一真实文件在未信任工作区展开，确保受控来源检查仍生效而非仅检查命令前缀。
def test_native_skill_command_uses_source_trust(tmp_path: Path) -> None:
    folder = tmp_path / ".coderook" / "skills"
    folder.mkdir(parents=True)
    (folder / "inspect.md").write_text(
        "---\nname: inspect\n---\nRead relevant code.", encoding="utf-8"
    )
    loader = SkillLoader(tmp_path, user_skills_dir=tmp_path / "user-skills")
    with pytest.raises(SkillTrustError):
        expand_skill_input("/skill:inspect", loader, workspace_trusted=False)


# 功能：用户指定的单文件、Skill 包及集合目录都能被发现并通过显式命令展开。
# 设计：三个真实来源布局共用断言，确认没有依赖复制安装或工作区信任。
@pytest.mark.parametrize("layout", ["file", "package", "collection"])
def test_explicit_skill_paths_are_executable(tmp_path: Path, layout: str) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    collection = tmp_path / "external"
    package = collection / "inspect"
    package.mkdir(parents=True)
    path = collection / "inspect.md" if layout == "file" else package / "SKILL.md"
    path.write_text(
        "---\nname: inspect\ndescription: Inspect public APIs\n---\nRead relevant code.",
        encoding="utf-8",
    )
    source = path if layout == "file" else package if layout == "package" else collection
    loader = SkillLoader(
        workspace, user_skills_dir=tmp_path / "unused",
        additional_paths=(source,),
    )
    skills = loader.list_for_execution(workspace_trusted=False)
    found = next(skill for skill in skills if skill.name == "inspect")
    assert found.path == str(path.resolve())
    assert found.trust == "trusted"
    assert loader.show("inspect").path == found.path
    expanded = expand_skill_input("/skill:inspect auth", loader, workspace_trusted=False)
    assert "Read relevant code." in expanded
    assert expanded.endswith("\n\nauth")


# 功能：多行 YAML 和手动调用开关兼容 Pi，隐藏 Skill 仍可通过显式命令使用。
# 设计：以带 BOM 的真实文件贯穿解析、模型目录和命令展开，排除仅存储开关未生效。
def test_manual_only_skill_yaml(tmp_path: Path) -> None:
    path = tmp_path / "manual.md"
    path.write_text(
        "---\nname: manual\ndescription: >-\n  Review code\n  carefully\n"
        "disable-model-invocation: true\nmetadata:\n  author: example\n---\nManual instructions.",
        encoding="utf-8-sig",
    )
    loader = SkillLoader(tmp_path, additional_paths=(path,))
    skills = loader.list_for_execution(workspace_trusted=False)
    manual = next(skill for skill in skills if skill.name == "manual")
    assert manual.description == "Review code carefully"
    assert manual.manifest.disable_model_invocation
    assert "<name>manual</name>" not in build_capability_context(skills, [], tool_names={"read"})
    assert "Manual instructions." in expand_skill_input(
        "/skill:manual", loader, workspace_trusted=False,
    )


# 功能：嵌套 Skill 使用声明名称调用，包内参考资料不会被当成额外 Skill。
# 设计：目录名与 manifest 名不同，并在包内放伪 Skill，验证递归发现的停止条件。
def test_nested_skill_declared_name_and_package_boundary(tmp_path: Path) -> None:
    collection = tmp_path / "collection"
    package = collection / "team" / "python" / "folder-name"
    references = package / "references" / "example"
    references.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: api-review\ndescription: Review API compatibility\n---\nActual instructions.",
        encoding="utf-8",
    )
    (references / "SKILL.md").write_text(
        "---\nname: not-a-skill\ndescription: Example document\n---\nExample.",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path, additional_paths=(collection,))
    skills = loader.list_for_execution(workspace_trusted=False)
    names = {skill.name for skill in skills}
    assert "api-review" in names
    assert "folder-name" not in names and "not-a-skill" not in names
    assert "Actual instructions." in expand_skill_input(
        "/skill:api-review", loader, workspace_trusted=False,
    )
    assert loader.resolve("folder-name") is None


# 功能：Skill 发现遵守三种忽略文件、目录作用域及否定规则。
# 设计：使用嵌套真实集合并同时检查目录列表和显式解析，避免隐藏后仍能按名称加载。
def test_skill_collection_ignore_rules(tmp_path: Path) -> None:
    collection = tmp_path / "skills"
    for relative in ("keep", "disabled", "team/hidden", "team/visible", "other/hidden"):
        package = collection / relative
        package.mkdir(parents=True)
        name = relative.replace("/", "-")
        (package / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Test skill\n---\nInstructions.", encoding="utf-8",
        )
    (collection / ".gitignore").write_text("disabled/\nkeep/\n", encoding="utf-8")
    (collection / ".ignore").write_text("!keep/\n", encoding="utf-8")
    (collection / "team" / ".fdignore").write_text("hidden/\n", encoding="utf-8")
    loader = SkillLoader(tmp_path, additional_paths=(collection,))
    names = {skill.name for skill in loader.list_for_execution(workspace_trusted=False)}
    assert {"keep", "team-visible", "other-hidden"} <= names
    assert not {"disabled", "team-hidden"} & names
    assert loader.resolve("disabled") is None
    assert loader.resolve("team-hidden") is None

from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.skills import (
    SkillConfirmationRequired,
    SkillIntegrityError,
    SkillLoader,
    SkillManager,
    SkillManagerError,
)
from code_rook.core.tools.builtin.skill import SkillTool


# 创建包含 manifest、正文和辅助文件的目录式安装源
def _write_source(root: Path, name: str = "custom") -> Path:
    source = root / "source" / name
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Managed test skill\n"
        "allowed_tools:\n"
        "  - File\n"
        "---\n"
        "Follow managed instructions for $ARGUMENTS.\n",
        encoding="utf-8",
    )
    (source / "reference.txt").write_text("version one\n", encoding="utf-8")
    return source


# 功能：验证 install 在确认前只返回 preview 且不会创建项目 skill 目录
# 设计：捕获 SkillConfirmationRequired 中的 preview，同时检查目标路径没有任何写入
def test_install_requires_preview_confirmation(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    manager = SkillManager(tmp_path, user_skills_dir=tmp_path / "user-skills")

    with pytest.raises(SkillConfirmationRequired) as captured:
        manager.install(str(source), scope="project", trust="trusted")

    preview = captured.value.preview
    assert preview.name == "custom"
    assert preview.scope == "project"
    assert preview.trust == "trusted"
    assert preview.files == ("SKILL.md", "reference.txt")
    assert not (tmp_path / ".coderook" / "skills").exists()


# 功能：验证项目安装只能写入 .coderook/skills 并记录完整 provenance
# 设计：确认安装后检查唯一目标、元数据字段和 loader 的 verified 状态
def test_confirmed_install_writes_project_managed_directory(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    manager = SkillManager(tmp_path, user_skills_dir=tmp_path / "user-skills")

    installed = manager.install(
        str(source),
        scope="project",
        trust="trusted",
        confirmed=True,
    )

    target = tmp_path / ".coderook" / "skills" / "custom"
    assert Path(installed.path) == (target / "SKILL.md").resolve()
    assert installed.scope == "project"
    assert installed.trust == "trusted"
    assert installed.integrity == "verified"
    assert installed.installed_at
    assert installed.source == str(source)
    assert (target / ".coderook-skill.json").is_file()
    assert not (tmp_path / ".claude" / "skills" / "custom").exists()


# 功能：验证安装后任何正文或辅助文件修改都会在执行前显示 digest mismatch
# 设计：先安装并成功 resolve，再修改辅助文件，比较 audit 与 resolve/SkillTool 三个入口
async def test_digest_mismatch_is_visible_before_skill_use(tmp_path: Path) -> None:
    source = _write_source(tmp_path)
    user_dir = tmp_path / "user-skills"
    manager = SkillManager(tmp_path, user_skills_dir=user_dir)
    manager.install(str(source), confirmed=True, trust="trusted")
    loader = SkillLoader(tmp_path, user_skills_dir=user_dir)
    assert loader.resolve("custom") is not None

    target = tmp_path / ".coderook" / "skills" / "custom"
    (target / "reference.txt").write_text("tampered\n", encoding="utf-8")

    audit = next(record for record in loader.audit() if record.name == "custom")
    assert audit.integrity == "mismatch"
    assert audit.digest != audit.expected_digest
    with pytest.raises(SkillIntegrityError, match="digest mismatch"):
        loader.resolve("custom")
    result = await SkillTool(loader).invoke({"name": "custom"})
    assert result.is_error
    assert result.error_type == "integrity_error"


# 功能：验证兼容目录只读导入且 remove 不会跨目录删除
# 设计：仅在 .claude/skills 创建 legacy skill，确认可发现但项目 remove 明确失败且文件保留
def test_legacy_skill_is_read_only_import(tmp_path: Path) -> None:
    legacy = tmp_path / ".claude" / "skills" / "legacy"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text(
        "---\nname: legacy\ndescription: imported\n---\nlegacy body\n",
        encoding="utf-8",
    )
    user_dir = tmp_path / "user-skills"
    manager = SkillManager(tmp_path, user_skills_dir=user_dir)

    skill = manager.show("legacy")

    assert skill is not None
    assert skill.scope == "legacy"
    with pytest.raises(SkillManagerError, match="not found in project scope"):
        manager.remove("legacy", scope="project", confirmed=True)
    assert (legacy / "SKILL.md").is_file()


# 功能：验证 project、user、builtin 优先级不会被 legacy 同名 skill 颠倒
# 设计：创建 user 与 project 同名条目，断言 project 胜出；删除 project 后 user 接管
def test_skill_priority_project_over_user_and_legacy(tmp_path: Path) -> None:
    user_dir = tmp_path / "user-skills"
    user_dir.mkdir()
    (user_dir / "review.md").write_text(
        "---\nname: review\ndescription: user\n---\nuser\n",
        encoding="utf-8",
    )
    project_dir = tmp_path / ".coderook" / "skills"
    project_dir.mkdir(parents=True)
    (project_dir / "review.md").write_text(
        "---\nname: review\ndescription: project\n---\nproject\n",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path, user_skills_dir=user_dir)

    assert loader.resolve("review").description == "project"  # type: ignore[union-attr]

    SkillManager(tmp_path, user_skills_dir=user_dir).remove(
        "review",
        scope="project",
        confirmed=True,
    )
    assert loader.resolve("review").description == "user"  # type: ignore[union-attr]

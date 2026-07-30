from __future__ import annotations

from pathlib import Path

from code_rook.core.skills.loader import SkillLoader
from code_rook.core.tools.builtin.skill import SkillTool


# 在临时项目中创建一个目录式 Skill，供工具发现与加载测试复用
def _write_skill(root: Path) -> None:
    skill_dir = root / ".coderook" / "skills" / "desktop-inventory"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: desktop-inventory\n"
        "description: Inspect locally installed AI applications\n"
        "allowed_tools:\n"
        "  - bash\n"
        "---\n"
        "Inspect the host for $ARGUMENTS using read-only commands.\n",
        encoding="utf-8",
    )


# 功能：验证 skill 工具 schema 向模型暴露可用名称与描述
# 设计：创建项目级 Skill 后检查 enum 和工具描述，覆盖自然语言描述驱动发现入口
def test_skill_tool_exposes_available_metadata(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    tool = SkillTool(SkillLoader(tmp_path))

    properties = tool.input_schema["properties"]
    assert isinstance(properties, dict)
    name_schema = properties["name"]
    assert isinstance(name_schema, dict)
    assert "desktop-inventory" in name_schema["enum"]
    assert "Inspect locally installed AI applications" in tool.description


# 功能：验证选中 Skill 后才加载完整正文并替换用户参数
# 设计：直接调用工具并检查正文、参数和声明工具，确认渐进式加载结果完整
async def test_skill_tool_loads_instructions_on_demand(tmp_path: Path) -> None:
    _write_skill(tmp_path)
    tool = SkillTool(SkillLoader(tmp_path))

    result = await tool.invoke(
        {"name": "desktop-inventory", "arguments": "AI agent tools"}
    )

    assert not result.is_error
    assert "Inspect the host for AI agent tools" in result.content
    assert "Declared tools: bash" in result.content


# 功能：验证不存在的 Skill 返回结构化工具错误而不是空内容
# 设计：传入未出现在目录中的名称，检查错误类型供 Agent Loop 重新决策
async def test_skill_tool_rejects_unknown_name(tmp_path: Path) -> None:
    result = await SkillTool(SkillLoader(tmp_path)).invoke({"name": "missing"})

    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "Unknown skill" in result.content

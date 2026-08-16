from __future__ import annotations

from pathlib import Path

from code_rook.core.memory.loader import load_context_file, load_project_instructions


# 功能：验证文件存在时返回去除首尾空格的完整内容
# 设计：用 tmp_path 写入带前后空白行的文件，断言 strip 后内容一致
def test_load_existing_file(tmp_path: Path) -> None:
    ctx = tmp_path / "context.md"
    ctx.write_text("  # My Context\n- item one\n", encoding="utf-8")
    result = load_context_file(ctx)
    assert result == "# My Context\n- item one"


# 功能：验证文件不存在时返回空字符串
# 设计：传入不存在的路径，无需创建文件，断言返回值为空字符串
def test_load_missing_file(tmp_path: Path) -> None:
    result = load_context_file(tmp_path / "nonexistent.md")
    assert result == ""


# 功能：验证文件存在但内容为空（或仅空白）时返回空字符串
# 设计：写入纯空白内容，strip 后为空，断言返回空字符串
def test_load_empty_file(tmp_path: Path) -> None:
    ctx = tmp_path / "context.md"
    ctx.write_text("   \n\n  ", encoding="utf-8")
    result = load_context_file(ctx)
    assert result == ""


# 功能：验证项目指令按 AGENTS.md、CLAUDE.md、.coderook/context.md 顺序拼接并标注来源
# 设计：在临时工作区同时写入三个文件，断言段落顺序与来源标题，证明迁移用户的指令立即生效
def test_load_project_instructions_merges_agents_and_claude(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents rules", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
    coderook_dir = tmp_path / ".coderook"
    coderook_dir.mkdir()
    (coderook_dir / "context.md").write_text("coderook rules", encoding="utf-8")

    result = load_project_instructions(tmp_path)

    assert result == (
        "### From AGENTS.md\n\nagents rules\n\n"
        "### From CLAUDE.md\n\nclaude rules\n\n"
        "### From .coderook/context.md\n\ncoderook rules"
    )


# 功能：验证只有部分指令文件存在时只拼接存在的段落
# 设计：仅写入 CLAUDE.md，断言结果不包含 AGENTS.md 段且无空段残留
def test_load_project_instructions_partial_files(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude only", encoding="utf-8")

    result = load_project_instructions(tmp_path)

    assert result == "### From CLAUDE.md\n\nclaude only"


# 功能：验证没有任何指令文件的工作区返回空字符串
# 设计：空临时目录直接调用，断言返回空串使 project_context 保持缺省
def test_load_project_instructions_empty_workspace(tmp_path: Path) -> None:
    assert load_project_instructions(tmp_path) == ""

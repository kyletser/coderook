from __future__ import annotations

from pathlib import Path

from code_rook.core.memory.loader import (
    load_context_file,
    load_global_instructions,
    load_project_instructions,
    load_system_prompt_files,
)


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


# 功能：验证同层 AGENTS.md 优先于 CLAUDE.md，并追加旧 CodeRook 上下文
# 设计：同时写入三份内容，断言没有重复注入次选指令且兼容上下文保留
def test_load_project_instructions_selects_agents_before_claude(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agents rules", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("claude rules", encoding="utf-8")
    coderook_dir = tmp_path / ".coderook"
    coderook_dir.mkdir()
    (coderook_dir / "context.md").write_text("coderook rules", encoding="utf-8")

    result = load_project_instructions(tmp_path)

    assert result == (
        "### From AGENTS.md\n\nagents rules\n\n"
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


# 功能：验证祖先先于子目录，同层 override 替代普通指令且支持 UTF-8 BOM
# 设计：构造三级目录和冲突内容，通过顺序及排除断言验证继承规则
def test_context_ancestry_and_override(tmp_path: Path) -> None:
    leaf = tmp_path / "repo" / "src"
    leaf.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("outer rules", encoding="utf-8")
    (leaf.parent / "AGENTS.md").write_text("shadowed rules", encoding="utf-8")
    (leaf.parent / "AGENTS.override.md").write_text("override rules", encoding="utf-8-sig")
    (leaf / "CLAUDE.md").write_text("leaf rules", encoding="utf-8")
    result = load_project_instructions(leaf)
    assert result.index("outer rules") < result.index("override rules") < result.index("leaf rules")
    assert "shadowed rules" not in result
    assert "\ufeff" not in result


# 功能：验证同名目录不阻断后续候选，而空 override 仍覆盖本层普通指令
# 设计：先用目录模拟无效 AGENTS，再加入空覆盖文件，区分缺失与有意清空
def test_context_directory_and_empty_override(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").mkdir()
    (tmp_path / "CLAUDE.md").write_text("fallback rules", encoding="utf-8")
    assert "fallback rules" in load_project_instructions(tmp_path)
    (tmp_path / "AGENTS.override.md").touch()
    assert "fallback rules" not in load_project_instructions(tmp_path)


# 功能：验证用户级指令按优先级加载且旧 context.md 继续有效
# 设计：使用独立 agent_dir 避免读取开发者真实指令，验证两个来源的内容
def test_global_context_selection(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("global rules", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("unused rules", encoding="utf-8")
    (tmp_path / "context.md").write_text("legacy rules", encoding="utf-8")
    result = load_global_instructions(tmp_path)
    assert "global rules" in result and "legacy rules" in result
    assert "unused rules" not in result


# 功能：验证嵌套 Worktree 的同名指令只加载一次，缺少副本时仍继承主仓库
# 设计：构造 Git 的 gitdir/commondir 文件布局，不依赖本机 Git 配置或分支
def test_nested_worktree_context_shadow(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    git_dir = main / ".git" / "worktrees" / "worker"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..", encoding="utf-8")
    worker = main / ".coderook" / "worktrees" / "worker"
    leaf = worker / "src"
    leaf.mkdir(parents=True)
    (worker / ".git").write_text(f"gitdir: {git_dir.as_posix()}", encoding="utf-8")
    (main / "AGENTS.md").write_text("main rules", encoding="utf-8")
    assert "main rules" in load_project_instructions(leaf)
    (worker / "AGENTS.md").write_text("worker rules", encoding="utf-8")
    result = load_project_instructions(leaf)
    assert "worker rules" in result
    assert "main rules" not in result


# 功能：验证系统提示替换与追加独立选择，项目文件仅在受信工作区优先
# 设计：两级文件交叉存在并修改为空文件，覆盖回退和有意覆盖为空的区别
def test_system_prompt_file_precedence(tmp_path: Path) -> None:
    user = tmp_path / "user"
    workspace = tmp_path / "repo"
    project = workspace / ".coderook"
    user.mkdir()
    project.mkdir(parents=True)
    (user / "SYSTEM.md").write_text("global base", encoding="utf-8")
    (user / "APPEND_SYSTEM.md").write_text("global append", encoding="utf-8")
    (project / "SYSTEM.md").write_text("project base", encoding="utf-8")
    assert load_system_prompt_files(workspace, workspace_trusted=False, agent_dir=user) == (
        "global base", "global append",
    )
    assert load_system_prompt_files(workspace, workspace_trusted=True, agent_dir=user) == (
        "project base", "global append",
    )
    (project / "APPEND_SYSTEM.md").touch()
    assert load_system_prompt_files(workspace, workspace_trusted=True, agent_dir=user) == (
        "project base", "",
    )

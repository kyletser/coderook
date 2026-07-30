from __future__ import annotations

from code_rook.core.context import ExecutionContext


def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = dict(run_id="r1", goal="test goal", max_steps=5)
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# 功能：验证三层记忆全部存在时都出现在 system prompt 中且顺序正确
# 设计：分别设置 global_context、project_context、session_notes，断言各 section 标题及内容依次出现
def test_all_layers_present() -> None:
    ctx = _make_ctx(
        global_context="global line",
        project_context="project line",
        session_notes="session note",
    )
    prompt = ctx.system_prompt("BASE")
    assert "BASE" in prompt
    assert "## Global Context\nglobal line" in prompt
    assert "## Project Context\nproject line" in prompt
    assert "## Session Notes\nsession note" in prompt
    # 顺序：global 在 project 之前，project 在 session 之前
    assert prompt.index("Global") < prompt.index("Project") < prompt.index("Session")


# 功能：验证没有记忆层时仍始终注入内部英文、用户回复跟随语言的策略
# 设计：不设置任何可选上下文，断言 base 保留且语言策略存在
def test_no_layers() -> None:
    ctx = _make_ctx()
    prompt = ctx.system_prompt("BASE_ONLY")
    assert prompt.startswith("BASE_ONLY")
    assert "## Language Policy" in prompt
    assert "Use concise English for internal reasoning" in prompt
    assert "natural language of the user's latest message" in prompt
    assert "default to Simplified Chinese" in prompt


# 功能：验证只有 global_context 时只出现 Global section，其他 section 不出现
# 设计：只设置 global_context，断言 Project 和 Session 标题不在 prompt 中
def test_only_global() -> None:
    ctx = _make_ctx(global_context="global content")
    prompt = ctx.system_prompt("BASE")
    assert "## Global Context" in prompt
    assert "## Project Context" not in prompt
    assert "## Session Notes" not in prompt


# 功能：验证 session_notes 非空时包含 note_save 提示语
# 设计：只设置 session_notes，断言 prompt 含 note_save 相关提示
def test_session_notes_hint() -> None:
    ctx = _make_ctx(session_notes="some note")
    prompt = ctx.system_prompt("BASE")
    assert "note_save" in prompt


# 功能：验证运行环境和扩展能力目录会在记忆层之前注入系统提示
# 设计：同时设置两个新上下文字段与 global context，断言分区内容和稳定顺序
def test_runtime_and_capability_context_precede_memory() -> None:
    ctx = _make_ctx(
        runtime_context="Windows workspace",
        capability_context="skill review",
        global_context="global rule",
    )
    prompt = ctx.system_prompt("BASE")
    assert "## Runtime Environment\nWindows workspace" in prompt
    assert "## Available Extensions\nskill review" in prompt
    assert prompt.index("Runtime Environment") < prompt.index("Available Extensions")
    assert prompt.index("Available Extensions") < prompt.index("Global Context")


# 功能：验证显式 Skill 或 Subagent 覆盖基础提示时仍保留统一语言策略
# 设计：设置 system_prompt_override 并传入不同 base，断言只替换角色提示而不绕过语言约束
def test_language_policy_survives_system_prompt_override() -> None:
    ctx = _make_ctx(system_prompt_override="SPECIALIZED ROLE")

    prompt = ctx.system_prompt("DEFAULT ROLE")

    assert prompt.startswith("SPECIALIZED ROLE")
    assert "DEFAULT ROLE" not in prompt
    assert "## Language Policy" in prompt
    assert "default to Simplified Chinese" in prompt

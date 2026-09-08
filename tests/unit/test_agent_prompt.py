from code_rook.core.agent_runtime.prompt import build_system_prompt


# 功能：提示只描述当前工具，不注入不存在的工具名和额外任务编排要求。
# 设计：分别使用只有读取工具和空目录的输入，检查默认提示与真实能力一致。
def test_prompt_tracks_actual_tool_catalog() -> None:
    prompt = build_system_prompt([{"name": "read", "description": "Read a file"}])
    assert "- read: Read a file" in prompt
    assert "Use the available shell" not in prompt
    assert "note_save" not in prompt
    assert "tasks" not in prompt
    assert "Do not emit progress" not in prompt
    assert "Available tools:\n(none)" in build_system_prompt([])


# 功能：Shell 使用指引只在实际有 Shell 工具时出现。
# 设计：分别覆盖新小写工具名与现有 family 名，保证移植期间不丢失能力提示。
def test_prompt_shell_guideline_matches_available_tool() -> None:
    for name in ("bash", "Bash", "powershell"):
        prompt = build_system_prompt([{"name": name, "description": "Execute commands"}])
        assert "Use the available shell" in prompt
        assert f"- {name}: Execute commands" in prompt


# 功能：工具可自定义短说明和规则，相同规则只出现一次且不修改原 schema
# 设计：用两个工具声明重复规则，直接验证提示内容和源数据彼此独立
def test_custom_tool_prompt_metadata() -> None:
    tools = [{"name": "custom", "description": "Long schema description",
              "prompt_snippet": "Short purpose", "prompt_guidelines": (" Use once. ",)},
             {"name": "second", "description": "Second tool",
              "prompt_guidelines": ("Use once.", "", "Check result.")}]
    prompt = build_system_prompt(tools)
    assert "- custom: Short purpose" in prompt
    assert "Long schema description" not in prompt
    assert prompt.count("Use once.") == 1
    assert "Check result." in prompt
    assert tools[0]["description"] == "Long schema description"

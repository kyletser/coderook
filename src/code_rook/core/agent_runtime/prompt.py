# Python prompt assembly adapted from Pi system-prompt.ts (MIT; vendor/pi/LICENSE).
from typing import Any


# 从实际发送给模型的 JSON Schema 中提取当前可调用的 action
def _schema_actions(tool: dict[str, Any]) -> tuple[str, ...]:
    input_schema = tool.get("input_schema")
    if not isinstance(input_schema, dict):
        return ()
    variants = input_schema.get("oneOf")
    candidates = variants if isinstance(variants, list) else [input_schema]
    actions: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            continue
        action = properties.get("action")
        if not isinstance(action, dict):
            continue
        values = action.get("enum")
        if isinstance(values, list):
            actions.extend(str(value) for value in values if str(value))
    return tuple(dict.fromkeys(actions))


# 仅按当前模型可用的工具生成说明，不要求虚构阶段、任务板或额外分类。
def build_system_prompt(tools: list[dict[str, Any]]) -> str:
    names = {str(tool.get("name", "")) for tool in tools}
    actions_by_name = {
        str(tool.get("name", "")): _schema_actions(tool)
        for tool in tools
        if tool.get("name")
    }
    snippets: list[str] = []
    for tool in tools:
        name = str(tool.get("name", ""))
        if not name:
            continue
        actions = actions_by_name.get(name, ())
        if actions:
            snippets.append(
                f"- {name}: Available actions: {', '.join(actions)}. Use only these actions."
            )
            continue
        description = ' '.join(
            str(tool.get("prompt_snippet") or tool.get("description", "")).split()
        )[:240]
        snippets.append(f"- {name}: {description}")
    guidelines = [
        "Be concise in your responses.",
        "Show file paths clearly when working with files.",
    ]
    role = (
        "You are an expert coding assistant operating inside CodeRook, a local coding agent. "
        "Use only the tools and actions listed below to complete the user's request."
    )
    if not names:
        role = (
            "You are an expert coding assistant operating inside CodeRook, a local coding agent. "
            "Answer directly from the user's request and any context already provided."
        )
        guidelines.append(
            "No tools are available. Do not emit tool calls or tool-call markup; explain any "
            "missing information plainly."
        )
    if "read" in names:
        guidelines.append("Use read to examine files instead of cat or sed.")
    if "edit" in names:
        guidelines.append(
            "Use edit for precise changes. Match edits[].oldText against the original file. "
            "Combine disjoint changes to one file in a single call; do not overlap replacements."
        )
    shell_names = names & {"bash", "Bash", "powershell"}
    if any(
        not actions_by_name.get(name) or "run" in actions_by_name[name]
        for name in shell_names
    ):
        guidelines.append("Use the available shell for commands, searches, builds and tests.")
    for tool in tools:
        for guideline in tool.get("prompt_guidelines", ()):
            normalized = str(guideline).strip()
            if normalized and normalized not in guidelines:
                guidelines.append(normalized)
    return (
        role + "\n\nAvailable tools:\n" + ("\n".join(snippets) or "(none)")
        + "\n\nGuidelines:\n" + "\n".join(f"- {item}" for item in guidelines)
    )

# Python prompt assembly adapted from Pi system-prompt.ts (MIT; vendor/pi/LICENSE).
from typing import Any


# 仅按当前模型可用的工具生成说明，不要求虚构阶段、任务板或额外分类。
def build_system_prompt(tools: list[dict[str, Any]]) -> str:
    names = {str(tool.get("name", "")) for tool in tools}
    snippets = [
        f"- {tool['name']}: "
        + ' '.join(str(tool.get("prompt_snippet") or tool.get("description", "")).split())[:240]
        for tool in tools
        if tool.get("name")
    ]
    guidelines = [
        "Be concise in your responses.",
        "Show file paths clearly when working with files.",
    ]
    if "read" in names:
        guidelines.append("Use read to examine files instead of cat or sed.")
    if "edit" in names:
        guidelines.append(
            "Use edit for precise changes. Match edits[].oldText against the original file. "
            "Combine disjoint changes to one file in a single call; do not overlap replacements."
        )
    if names & {"bash", "Bash", "powershell"}:
        guidelines.append("Use the available shell for commands, searches, builds and tests.")
    for tool in tools:
        for guideline in tool.get("prompt_guidelines", ()):
            normalized = str(guideline).strip()
            if normalized and normalized not in guidelines:
                guidelines.append(normalized)
    return (
        "You are an expert coding assistant operating inside CodeRook, a local coding agent. "
        "You help users by reading files, executing commands, editing code, and writing new files."
        "\n\nAvailable tools:\n" + ("\n".join(snippets) or "(none)")
        + "\n\nGuidelines:\n" + "\n".join(f"- {item}" for item in guidelines)
    )

from __future__ import annotations

import os
import platform
from html import escape
from pathlib import Path

from code_rook.core.agents.loader import AgentProfile
from code_rook.core.skills.models import Skill


# 把能力描述压缩成安全的单行元数据，避免目录内容无界占用上下文
def _summary(value: str, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


# 构建不含用户隐私和环境变量值的本机执行环境摘要
def build_runtime_context(workspace_root: Path, *, command_shell: str | None = None) -> str:
    if command_shell is not None:
        shell = command_shell
    elif os.name == "nt":
        shell = Path(os.environ.get("COMSPEC", "cmd.exe")).name
    else:
        shell = Path(os.environ.get("SHELL", "sh")).name
    return "\n".join(
        [
            f"- Operating system: {platform.system()} {platform.release()}",
            f"- Working directory: {workspace_root.resolve()}",
            f"- Command shell: {shell}",
            (
                "- File writes are confined to the working directory. The read tool can also "
                "read enabled skill directories and their reference files. The bash tool runs "
                "host shell commands from that directory and can use locally available "
                "command-line programs and operating-system utilities."
            ),
            (
                "- Some shell commands or paths may require user approval. A required "
                "approval is not the same as lacking the capability."
            ),
        ]
    )


# 构建供模型按描述自动选择的 skill 与 subagent 元数据目录
def build_capability_context(
    skills: list[Skill],
    agents: list[AgentProfile],
    *,
    tool_names: set[str] | None = None,
) -> str:
    skills = [skill for skill in skills if not skill.manifest.disable_model_invocation]
    if tool_names is not None and "skill" not in tool_names:
        parts: list[str] = []
        if skills and tool_names.intersection({"read", "bash"}):
            parts.extend([
                "The following skills provide specialized instructions for specific tasks.",
                "Load a skill's file only when the task matches its description.",
                "Use read to load the listed skill file and its referenced files. "
                "Normal tool approval rules still apply.",
                "Resolve relative paths in instructions against the skill file's directory.",
                "<available_skills>",
            ])
            for skill in sorted(skills, key=lambda item: item.name):
                parts.extend([
                    "  <skill>",
                    f"    <name>{escape(skill.name)}</name>",
                    f"    <description>{escape(_summary(skill.description))}</description>",
                    f"    <location>{escape(skill.path)}</location>",
                    "  </skill>",
                ])
            parts.append("</available_skills>")
        if "agent" in tool_names and agents:
            parts.append("Use agent action=start only for self-contained delegated tasks.")
            parts.extend(
                f"- {agent.name}: {_summary(agent.description)}"
                for agent in sorted(agents, key=lambda item: item.name)
            )
        return "\n".join(parts)
    lines = [
        "Skills are reusable instructions. Call the skill tool when a request matches "
        "a skill description; its full instructions load only after selection.",
        "Available skills:",
    ]
    if skills:
        lines.extend(
            f"- {skill.name} [integrity={skill.integrity}, trust={skill.trust}]: "
            f"{_summary(skill.description) or 'No description provided.'}"
            for skill in sorted(skills, key=lambda item: item.name)
        )
    else:
        lines.append("- None")

    if tool_names is not None and "agent" not in tool_names:
        return "\n".join(lines)

    lines.extend(
        [
            "",
            (
                "Subagents are isolated workers. Use agent action=start only for a "
                "self-contained delegated task whose description matches a profile. "
                "Use status, peek, wait, cancel, and followup for its durable lifecycle."
            ),
            "Available subagent profiles:",
        ]
    )
    if agents:
        lines.extend(
            f"- {agent.name}: {_summary(agent.description) or 'No description provided.'}"
            for agent in sorted(agents, key=lambda item: item.name)
        )
    else:
        lines.append("- None")
    return "\n".join(lines)

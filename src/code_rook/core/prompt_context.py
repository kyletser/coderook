from __future__ import annotations

import os
import platform
from pathlib import Path

from code_rook.core.agents.loader import AgentProfile
from code_rook.core.skills.loader import Skill


# 把能力描述压缩成安全的单行元数据，避免目录内容无界占用上下文
def _summary(value: str, limit: int = 240) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


# 构建不含用户隐私和环境变量值的本机执行环境摘要
def build_runtime_context(workspace_root: Path) -> str:
    if os.name == "nt":
        shell = Path(os.environ.get("COMSPEC", "cmd.exe")).name
    else:
        shell = Path(os.environ.get("SHELL", "sh")).name
    return "\n".join(
        [
            f"- Operating system: {platform.system()} {platform.release()}",
            f"- Working directory: {workspace_root.resolve()}",
            f"- Command shell: {shell}",
            (
                "- File tools are confined to the working directory. The bash tool runs "
                "host shell commands from that directory and can inspect local processes, "
                "installed applications, package managers, and other system information."
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
) -> str:
    lines = [
        "Skills are reusable instructions. Call the skill tool when a request matches "
        "a skill description; its full instructions load only after selection.",
        "Available skills:",
    ]
    if skills:
        lines.extend(
            f"- {skill.name}: {_summary(skill.description) or 'No description provided.'}"
            for skill in sorted(skills, key=lambda item: item.name)
        )
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            (
                "Subagents are isolated workers. Use spawn_agent only for a self-contained "
                "delegated task whose description matches a profile."
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

from __future__ import annotations

from typing import Literal

from code_rook.cli.commands.run import cmd_run
from code_rook.core.config import CodeRookConfig

ReviewOutputFormat = Literal["text", "json", "stream-json"]

_READ_ONLY_TOOLS = ["read_file", "list_dir", "glob", "grep", "git_diff", "Repository"]
_REVIEW_CONTRACT = """

Read-only review contract:
- Do not modify files, run mutating commands, or change external state.
- Inspect repository evidence before drawing conclusions. Do not invent findings.
- Final response must contain: Summary; Findings ordered by P0-P3 severity; Risks and
  unknowns; Verification performed.
- Every finding must include severity, file and line when available, concrete evidence,
  user impact, and a specific recommendation. If no actionable defect is found, state that
  explicitly and keep residual risks separate from findings.
""".strip()


# 将用户审查目标与稳定的只读结构化输出契约组合
def build_review_goal(goal: str) -> str:
    return f"{goal.strip()}\n\n{_REVIEW_CONTRACT}"


# 使用 headless allow-list 执行不可写的结构化代码审查
def cmd_review(
    goal: str,
    config: CodeRookConfig,
    *,
    output_format: ReviewOutputFormat = "text",
) -> None:
    cmd_run(
        build_review_goal(goal),
        config,
        permission_mode="allow_list",
        allow_tools=list(_READ_ONLY_TOOLS),
        output_format=output_format,
        question_mode="preset",
        preset_answers=["保持只读；基于当前仓库证据完成审查。"],
    )

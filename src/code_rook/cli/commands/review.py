from __future__ import annotations

from typing import Literal

from code_rook.cli.commands.run import cmd_run
from code_rook.core.config import CodeRookConfig
from code_rook.core.review import build_review_goal

ReviewOutputFormat = Literal["text", "json", "stream-json"]

_READ_ONLY_TOOLS = ["read_file", "list_dir", "glob", "grep", "git_diff", "Repository"]
_READ_ONLY_MODEL_TOOLS = ["read"]
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
        model_tools=list(_READ_ONLY_MODEL_TOOLS),
        output_format=output_format,
        question_mode="preset",
        preset_answers=["保持只读；基于当前仓库证据完成审查。"],
    )

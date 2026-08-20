from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

_READ_ONLY_TOOLS = ("read_file", "glob", "grep", "git_diff", "Repository")


# 构造只允许读取、搜索和查看 diff 的 headless 审查命令
def build_command(goal: str) -> list[str]:
    review_goal = (
        goal
        + "\n\n按 Summary、Findings(P0-P3)、Risks and unknowns、"
        "Verification performed 输出；每项 finding 必须包含文件位置、证据、影响和建议。"
    )
    command = [
        "coderook",
        "run",
        "--goal",
        review_goal,
        "--output-format",
        "stream-json",
        "--permission-mode",
        "allow-list",
        "--question-mode",
        "preset",
        "--answer",
        "不要修改文件；基于现有证据完成只读审查。",
    ]
    for tool in _READ_ONLY_TOOLS:
        command.extend(("--allow-tool", tool))
    return command


# 解析目标并执行或仅打印安全审查命令
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run a read-only CodeRook review")
    parser.add_argument(
        "--goal",
        default="审查当前改动，按优先级报告真实缺陷并给出文件位置",
    )
    parser.add_argument("--print-command", action="store_true")
    args = parser.parse_args()
    command = build_command(args.goal)
    if args.print_command:
        print(shlex.join(command))
        return
    completed = subprocess.run(command, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()

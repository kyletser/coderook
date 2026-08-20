from __future__ import annotations

import argparse
import shlex
import subprocess
import sys

_FIX_TOOLS = (
    "read_file",
    "glob",
    "grep",
    "edit_file",
    "apply_patch",
    "git_diff",
    "Run.run",
    "Bash.run",
)


# 构造带显式工具白名单和有限提问答案的自动修复命令
def build_command(goal: str) -> list[str]:
    command = [
        "coderook",
        "run",
        "--goal",
        goal,
        "--output-format",
        "stream-json",
        "--permission-mode",
        "allow-list",
        "--question-mode",
        "preset",
        "--answer",
        "保持改动最小；不要访问工作区外路径；完成后运行相关测试。",
    ]
    for tool in _FIX_TOOLS:
        command.extend(("--allow-tool", tool))
    return command


# 解析目标并执行或仅打印受控自动修复命令
def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Run a controlled CodeRook fix")
    parser.add_argument(
        "--goal",
        default="修复当前测试失败，保持改动最小并运行相关测试",
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

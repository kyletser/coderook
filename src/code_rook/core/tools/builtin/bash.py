from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.processes import (
    bounded_shell_output,
    create_shell_process,
    terminate_process_tree,
)
from code_rook.core.tools.base import BaseTool, ToolResult

_DEFAULT_TIMEOUT = 60


class BashParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=120)


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a command in the host computer's local shell and return its output "
        "(stdout + stderr combined). Use it for project commands and host capabilities "
        "available through local command-line programs or operating-system utilities. "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Commands may require user approval. Prefer short, focused commands. "
        "Output is truncated at 64 KB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 初始化可选固定工作目录，供 worktree 隔离的 subagent 使用
    def __init__(self, cwd: Path | None = None) -> None:
        self._cwd = cwd

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        command = p.command
        timeout = p.timeout

        try:
            proc = await create_shell_process(command, self._cwd)
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except TimeoutError:
                await terminate_process_tree(proc)
                await proc.communicate()
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
            except asyncio.CancelledError:
                await asyncio.shield(terminate_process_tree(proc))
                await asyncio.shield(proc.communicate())
                raise
        except Exception as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output, _truncated = bounded_shell_output(stdout_bytes)

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=output or "[no output]")

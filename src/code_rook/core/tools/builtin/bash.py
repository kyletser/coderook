from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.persistent_shell import PersistentShellPool
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
    session: Literal["isolated", "persistent"] = "isolated"


class BashTool(BaseTool):
    params_model = BashParams
    name = "bash"
    description = (
        "Execute a command in the host computer's local shell and return its output "
        "(stdout + stderr combined). Use it for project commands and host capabilities "
        "available through local command-line programs or operating-system utilities. "
        "Non-interactive only — commands requiring user input will hang and time out. "
        "Commands may require user approval. Prefer short, focused commands. "
        "Output is truncated at 64 KB. "
        "Use session=persistent to keep cwd, environment variables and virtualenv "
        "activation across calls in the same chat session; session=isolated (default) "
        "runs each command in a fresh process."
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
            "session": {
                "type": "string",
                "enum": ["isolated", "persistent"],
                "description": (
                    "isolated: fresh process per command (default). "
                    "persistent: reuse a resident shell so cwd/env/venv persist "
                    "across calls in this chat session."
                ),
            },
        },
        "required": ["command"],
    }

    # 初始化可选固定工作目录与持久 shell 池，供 worktree 隔离 subagent 与会话级状态保持
    def __init__(
        self,
        cwd: Path | None = None,
        *,
        persistent_pool: PersistentShellPool | None = None,
        persistent_key: str = "",
    ) -> None:
        self._cwd = cwd
        self._persistent_pool = persistent_pool
        self._persistent_key = persistent_key

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        if p.session == "persistent" and self._persistent_pool is not None:
            if self._persistent_key:
                return await self._run_persistent(p.command, p.timeout)
            return ToolResult(
                content=(
                    "persistent shell requires a chat session; "
                    "re-run with session=isolated"
                ),
                is_error=True,
                error_type="schema_error",
            )
        return await self._run_isolated(p.command, p.timeout)

    # 走一次性子进程的原有执行路径
    async def _run_isolated(self, command: str, timeout: int) -> ToolResult:
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

    # 走常驻 shell 会话，保留 cwd/env/venv 激活状态
    async def _run_persistent(self, command: str, timeout: int) -> ToolResult:
        assert self._persistent_pool is not None
        session = self._persistent_pool.get_or_create(self._persistent_key, self._cwd)
        try:
            outcome = await session.run(command, timeout_s=timeout)
        except (OSError, RuntimeError) as exc:
            return ToolResult(
                content=f"[persistent shell error] {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        if outcome.timed_out:
            return ToolResult(
                content=f"[timeout after {timeout}s]\n{outcome.text}",
                is_error=True,
                error_type="timeout",
            )
        if outcome.died or outcome.exit_code is None:
            return ToolResult(
                content=(
                    "[persistent shell exited unexpectedly; state reset]\n"
                    + outcome.text
                ),
                is_error=True,
                error_type="runtime_error",
            )
        text = outcome.text or "[no output]"
        if outcome.truncated:
            text += "\n[truncated]"
        if outcome.exit_code != 0:
            return ToolResult(
                content=f"[exit {outcome.exit_code}]\n{text}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=text)

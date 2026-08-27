from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.artifacts import ArtifactStore
from code_rook.core.persistent_shell import PersistentShellPool, ShellRunOutcome
from code_rook.core.processes import (
    ProcessSupervisor,
    _decode_shell_output,
    terminate_process_tree,
    wait_for_process_leader,
)
from code_rook.core.sandbox.planner import (
    SandboxPlan,
    SandboxSpawnRequest,
    spawn_sandboxed_shell,
)
from code_rook.core.tools.base import BaseTool, ToolResult

_DEFAULT_TIMEOUT = 60
_READ_CHUNK_BYTES = 64 * 1024
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024


# 为截断的常驻 shell 预览附加可分页读取的完整 artifact 引用
def _render_persistent_output(outcome: ShellRunOutcome) -> str:
    text = outcome.text
    if not outcome.truncated:
        return text
    if outcome.output_artifact:
        return (
            text
            + f"\n[truncated preview; full output: {outcome.output_artifact}; "
            f"size={outcome.output_artifact_size}; use artifact_read with offset/limit]"
        )
    if outcome.output_artifact_error:
        return text + "\n[truncated preview; full output artifact unavailable]"
    return text + "\n[truncated preview; full output unavailable]"


# 分块读取合并输出并施加硬字节上限，返回是否因超限提前停止 drain
async def _read_bounded_output(
    process: asyncio.subprocess.Process,
) -> tuple[bytes, bool]:
    if process.stdout is None:
        return b"", False
    output = bytearray()
    while chunk := await process.stdout.read(_READ_CHUNK_BYTES):
        room = _MAX_CAPTURE_BYTES - len(output)
        if room <= 0:
            return bytes(output), True
        output.extend(chunk[:room])
        if len(chunk) > room:
            return bytes(output), True
    return bytes(output), False


# 并行等待 leader 与输出，leader 结束后先清理整个执行组再等待后台后代释放管道
async def _collect_isolated_output(
    process: asyncio.subprocess.Process,
    supervisor: ProcessSupervisor | None,
) -> tuple[bytes, bool, dict[str, object] | None]:
    output_task = asyncio.create_task(_read_bounded_output(process))
    wait_task = asyncio.create_task(wait_for_process_leader(process))
    try:
        done, _pending = await asyncio.wait(
            {output_task, wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if output_task in done:
            output, exceeded = output_task.result()
            if exceeded:
                return output, True, None
        await wait_task
        process_usage: dict[str, object] | None = None
        if supervisor is not None:
            process_usage = (await supervisor.terminate(process)).to_dict()
        else:
            await terminate_process_tree(process)
        if output_task.done():
            output, exceeded = output_task.result()
        else:
            output, exceeded = await output_task
        return output, exceeded, process_usage
    finally:
        for task in (output_task, wait_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(output_task, wait_task, return_exceptions=True)


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
        sandbox_plan: SandboxPlan | None = None,
        process_supervisor: ProcessSupervisor | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._cwd = cwd
        self._persistent_pool = persistent_pool
        self._persistent_key = persistent_key
        # 本会话施加的真实 OS 沙箱计划；degraded 或空时按原样执行（仅审计）
        self._sandbox_plan = sandbox_plan
        self._process_supervisor = process_supervisor
        self._artifact_store = artifact_store

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时或非零退出码时返回错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = BashParams.model_validate(params)
        if p.session == "persistent" and self._persistent_pool is not None:
            if self._persistent_key:
                return await self._run_persistent(p.command, p.timeout)
            return ToolResult(
                content=("persistent shell requires a chat session; re-run with session=isolated"),
                is_error=True,
                error_type="schema_error",
            )
        return await self._run_isolated(p.command, p.timeout)

    # 走一次性子进程的原有执行路径；有真实沙箱时对命令施加 OS 包裹
    async def _run_isolated(self, command: str, timeout: int) -> ToolResult:
        process_usage: dict[str, object] | None = None
        try:
            proc = await spawn_sandboxed_shell(
                self._sandbox_plan,
                SandboxSpawnRequest(
                    command=command,
                    label="isolated-shell",
                    cwd=self._cwd,
                ),
                self._process_supervisor,
            )
            try:
                # 同一个 deadline 同时约束管道 drain 与进程退出，避免 EOF 后 wait 永久悬挂
                async with asyncio.timeout(timeout):
                    stdout_bytes, output_exceeded, process_usage = (
                        await _collect_isolated_output(
                            proc,
                            self._process_supervisor,
                        )
                    )
                if output_exceeded:
                    if self._process_supervisor is not None:
                        process_usage = (await self._process_supervisor.terminate(proc)).to_dict()
                    else:
                        await terminate_process_tree(proc)
                    return ToolResult(
                        content=(
                            f"[output exceeded {_MAX_CAPTURE_BYTES} bytes; "
                            "process terminated without reporting success]"
                        ),
                        is_error=True,
                        error_type="output_limit",
                        process_usage=process_usage,
                    )
            except TimeoutError:
                if self._process_supervisor is not None:
                    process_usage = (await self._process_supervisor.terminate(proc)).to_dict()
                else:
                    await terminate_process_tree(proc)
                return ToolResult(
                    content=f"[timeout after {timeout}s]",
                    is_error=True,
                    error_type="timeout",
                    process_usage=process_usage,
                )
            except asyncio.CancelledError:
                if self._process_supervisor is not None:
                    await asyncio.shield(self._process_supervisor.terminate(proc))
                else:
                    await asyncio.shield(terminate_process_tree(proc))
                raise
        except Exception as exc:
            runner_failed = bool(
                self._sandbox_plan is not None and self._sandbox_plan.enforced
            )
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_type=(
                    "sandbox_runner_failed" if runner_failed else "runtime_error"
                ),
                sandbox_enforcement=(
                    self._sandbox_plan.enforcement
                    if self._sandbox_plan is not None
                    else "unavailable"
                ),
            )

        output = _decode_shell_output(stdout_bytes)

        returncode = proc.returncode or 0
        if returncode != 0:
            runner_failed = bool(
                self._sandbox_plan is not None
                and self._sandbox_plan.capability.kind == "windows_acl"
                and returncode == 127
                and output.lstrip().startswith("windows-acl-run:")
            )
            return ToolResult(
                content=f"[exit {returncode}]\n{output}",
                is_error=True,
                error_type=("sandbox_runner_failed" if runner_failed else "nonzero_exit"),
                process_usage=process_usage,
                sandbox_enforcement=(
                    self._sandbox_plan.enforcement
                    if self._sandbox_plan is not None
                    else "unavailable"
                ),
            )
        return ToolResult(
            content=output or "[no output]",
            process_usage=process_usage,
            sandbox_enforcement=(
                self._sandbox_plan.enforcement
                if self._sandbox_plan is not None
                else "unavailable"
            ),
        )

    # 走常驻 shell 会话，保留 cwd/env/venv 激活状态
    async def _run_persistent(self, command: str, timeout: int) -> ToolResult:
        assert self._persistent_pool is not None
        session = self._persistent_pool.get_or_create(
            self._persistent_key,
            self._cwd,
            self._sandbox_plan,
            self._artifact_store,
        )
        try:
            outcome = await session.run(command, timeout_s=timeout)
        except (OSError, RuntimeError) as exc:
            return ToolResult(
                content=f"[persistent shell error] {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        rendered_output = _render_persistent_output(outcome)
        if outcome.timed_out:
            return ToolResult(
                content=f"[timeout after {timeout}s]\n{rendered_output}",
                is_error=True,
                error_type="timeout",
                process_usage=outcome.process_usage,
            )
        if outcome.died or outcome.exit_code is None:
            return ToolResult(
                content=(
                    "[persistent shell exited unexpectedly; state reset]\n"
                    + rendered_output
                ),
                is_error=True,
                error_type="runtime_error",
                process_usage=outcome.process_usage,
            )
        text = rendered_output or "[no output]"
        if outcome.exit_code != 0:
            runner_failed = bool(
                self._sandbox_plan is not None
                and self._sandbox_plan.capability.kind == "windows_acl"
                and outcome.exit_code == 127
                and text.lstrip().startswith("windows-acl-run:")
            )
            return ToolResult(
                content=f"[exit {outcome.exit_code}]\n{text}",
                is_error=True,
                error_type=("sandbox_runner_failed" if runner_failed else "nonzero_exit"),
                process_usage=outcome.process_usage,
                sandbox_enforcement=(
                    self._sandbox_plan.enforcement
                    if self._sandbox_plan is not None
                    else "unavailable"
                ),
            )
        return ToolResult(
            content=text,
            process_usage=outcome.process_usage,
            sandbox_enforcement=(
                self._sandbox_plan.enforcement
                if self._sandbox_plan is not None
                else "unavailable"
            ),
        )

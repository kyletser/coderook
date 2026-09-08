# Python shell execution and output accumulator adapted from Pi (MIT; vendor/pi/LICENSE).
from __future__ import annotations

import asyncio
import codecs
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, Field

from code_rook.core.processes import (
    ProcessSupervisor,
    terminate_process_tree,
    wait_for_process_leader,
)
from code_rook.core.sandbox.planner import SandboxPlan, SandboxSpawnRequest, spawn_sandboxed_shell
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.execution_metadata import report_tool_progress


# 优先选择 Git for Windows 的 Bash；避免把 cmd 命令解释器伪装成 Bash。
def bash_executable() -> str:
    candidates = (
        [
            str(Path(root) / "Git" / "bin" / "bash.exe")
            for name in ("ProgramFiles", "ProgramFiles(x86)")
            if (root := os.environ.get(name))
        ]
        if os.name == "nt"
        else ["/bin/bash"]
    )
    git = shutil.which("git")
    if os.name == "nt" and git:
        candidates.append(str(Path(git).parent.parent / "bin" / "bash.exe"))
    candidates.append(shutil.which("bash") or "")
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            if (
                os.name == "nt"
                and "windows/system32/bash.exe" in candidate.replace("\\", "/").lower()
            ):
                continue
            return candidate
    raise RuntimeError("Bash was not found. Install Git for Windows or add Bash to PATH.")


class OutputAccumulator:
    # 只在输出超过显示预算时落盘，内存始终保留有界原始缓冲及尾部文本。
    def __init__(self, directory: Path, max_bytes: int = 50 * 1024, max_lines: int = 2000) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.total_bytes = 0
        self.tail = ""
        self.path: Path | None = None
        self._raw = bytearray()
        self._file: BinaryIO | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    # 增量解码跨块 UTF-8，超过预算后保存完整字节并持续裁剪显示尾部。
    def append(self, data: bytes) -> None:
        self.total_bytes += len(data)
        self.tail += self._decoder.decode(data)
        overflow = (
            len(self.tail.encode("utf-8")) > self.max_bytes
            or len(self.tail.splitlines()) > self.max_lines
        )
        if self._file is None and overflow:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, filename = tempfile.mkstemp(
                prefix="shell-", suffix=".log", dir=self.directory
            )
            self._file = os.fdopen(descriptor, "wb")
            self.path = Path(filename)
            self._file.write(self._raw)
            self._raw.clear()
        if self._file is not None:
            self._file.write(data)
        else:
            self._raw.extend(data)
        self.tail = self.tail.encode("utf-8")[-self.max_bytes :].decode("utf-8", errors="ignore")
        lines = self.tail.splitlines(keepends=True)
        self.tail = "".join(lines[-self.max_lines :])

    # 结束输出流并关闭完整日志，路径保留供 read 或 Shell 按需查询。
    def close(self) -> None:
        self.tail += self._decoder.decode(b"", final=True)
        if self._file is not None:
            self._file.close()
            self._file = None


class ShellParams(BaseModel):
    command: str
    timeout: float | None = Field(default=None, gt=0, le=2_147_483.647)


class CodingShellTool(BaseTool):
    name = "bash"
    description = (
        "Execute Bash commands (Git Bash on Windows). Optional timeout in seconds; "
        "no default timeout. Large output is saved to a file; "
        "the result contains its tail and path. Current session metadata is available "
        "through CODEROOK_SESSION_ID, CODEROOK_PROVIDER, CODEROOK_MODEL, and "
        "CODEROOK_REASONING_LEVEL."
    )
    params_model = ShellParams
    input_schema = ShellParams.model_json_schema()
    timeout_s = 0.0

    # 冻结现有沙箱与进程管理服务，执行仍受统一审批管线管理。
    def __init__(
        self,
        cwd: Path,
        plan: SandboxPlan | None,
        supervisor: ProcessSupervisor | None,
        *,
        session_environment: Mapping[str, str] | None = None,
    ) -> None:
        self._cwd, self._plan, self._supervisor = cwd, plan, supervisor
        self._session_environment = dict(session_environment or {})

    # 启动实际 Bash 并流式收集输出，取消及超时都清理进程组。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = ShellParams.model_validate(params)
        output = OutputAccumulator(self._cwd / ".coderook" / "tool-output")
        process = await spawn_sandboxed_shell(
            self._plan,
            SandboxSpawnRequest(
                label="coding-bash",
                argv=(bash_executable(), "-c", request.command),
                cwd=self._cwd,
                env=self._session_environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            ),
            self._supervisor,
        )

        # 实时显示尾部，读取逻辑不要求换行也不把大输出常驻内存。
        async def read_output() -> None:
            assert process.stdout is not None
            while chunk := await process.stdout.read(64 * 1024):
                output.append(chunk)
                await report_tool_progress(output.tail[-4096:], output.total_bytes)

        reader = asyncio.create_task(read_output())
        timed_out = False
        try:
            async with asyncio.timeout(request.timeout):
                await wait_for_process_leader(process)
        except TimeoutError:
            timed_out = True
        finally:
            if self._supervisor is not None:
                await asyncio.shield(self._supervisor.terminate(process))
            else:
                await asyncio.shield(terminate_process_tree(process))
            try:
                await asyncio.wait_for(reader, timeout=5)
            finally:
                if not reader.done():
                    reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                output.close()
        content = output.tail or "[no output]"
        if output.path:
            content += (
                f"\n[Output truncated; full output: {output.path}; {output.total_bytes} bytes. "
                "Use read offset/limit to inspect.]"
            )
        failed = timed_out or process.returncode != 0
        if failed:
            content += (
                f"\n[timeout after {request.timeout}s]"
                if timed_out
                else f"\n[exit {process.returncode}]"
            )
        return ToolResult(
            content,
            is_error=failed,
            error_type="timeout" if timed_out else "nonzero_exit" if failed else None,
            process_usage={"exit_code": process.returncode},
            failure_category="timed_out" if timed_out else "command_failed" if failed else None,
            sandbox_enforcement=self._plan.enforcement if self._plan else "unavailable",
        )

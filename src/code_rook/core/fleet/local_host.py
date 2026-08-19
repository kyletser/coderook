from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from pydantic import JsonValue, ValidationError

from code_rook.core.fleet.models import LocalWorkerRequest
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.sandbox.planner import (
    SandboxPlan,
    SandboxSpawnRequest,
    spawn_sandboxed_shell,
)
from code_rook.core.workflow import WorkerExecutionResult


class LocalProcessHostError(RuntimeError):
    pass


# 从 subprocess stream 读取有界正文，超限立即失败
async def _read_bounded(
    stream: asyncio.StreamReader,
    *,
    limit: int,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(min(65_536, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise LocalProcessHostError(f"local worker output exceeds {limit} bytes")


class LocalProcessHost:
    # 固定可信 argv 和 workspace，workflow IR 无法注入 shell 或替换 executable
    def __init__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        output_limit: int = 1_048_576,
        process_supervisor: ProcessSupervisor | None = None,
        sandbox_plan: SandboxPlan | None = None,
    ) -> None:
        if not argv or not argv[0].strip():
            raise ValueError("local worker argv must not be empty")
        if output_limit < 1_024:
            raise ValueError("local worker output limit must be at least 1024 bytes")
        self._argv = argv
        self._cwd = cwd.resolve()
        self._output_limit = output_limit
        self._process_supervisor = process_supervisor or ProcessSupervisor()
        self._sandbox_plan = sandbox_plan

    # 通过 stdin/stdout 单请求 JSON 协议执行一个本地进程 Worker
    async def run(self, request: LocalWorkerRequest) -> WorkerExecutionResult:
        process = await spawn_sandboxed_shell(
            self._sandbox_plan,
            SandboxSpawnRequest(
                argv=self._argv,
                label="fleet-worker",
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            self._process_supervisor,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_bounded(process.stdout, limit=self._output_limit)
        )
        stderr_task = asyncio.create_task(
            _read_bounded(process.stderr, limit=self._output_limit)
        )
        try:
            process.stdin.write(request.model_dump_json().encode("utf-8") + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
            return_code = await process.wait()
        except BaseException:
            await self._process_supervisor.terminate(process)
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        process_usage = self._process_supervisor.forget(process).to_dict()
        if return_code != 0:
            diagnostic = stderr.decode("utf-8", errors="replace").strip()[:4_000]
            raise LocalProcessHostError(
                f"local worker exited {return_code}: {diagnostic or 'no diagnostic'}"
            )
        try:
            result = WorkerExecutionResult.model_validate_json(stdout.strip())
            receipt = dict(result.receipt)
            receipt["process_usage"] = cast(JsonValue, process_usage)
            return result.model_copy(update={"receipt": receipt})
        except (ValidationError, ValueError) as exc:
            raise LocalProcessHostError("local worker returned invalid result JSON") from exc

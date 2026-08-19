from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.hooks.models import HookConfig, HookPayload
from code_rook.core.processes import ProcessSupervisor


@dataclass(frozen=True)
class HookProcessResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool
    process_usage: dict[str, object] | None = None


# 持续排空子进程输出，仅保留配置允许的前若干字节
async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> tuple[bytes, bool]:
    if stream is None:
        return b"", False
    kept = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        remaining = limit - len(kept)
        if remaining > 0:
            kept.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated = True
    return bytes(kept), truncated


# 执行单个进程 hook，并对超时、输出和进程树实施硬边界
async def execute_hook_process(
    config: HookConfig,
    payload: HookPayload,
    *,
    cwd: Path,
    supervisor: ProcessSupervisor | None = None,
) -> HookProcessResult:
    owner = supervisor or ProcessSupervisor()
    process = await owner.start_exec(
        *config.command,
        label=f"hook:{config.id}",
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(payload.model_dump_json().encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, config.max_output_bytes))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, config.max_output_bytes))
    wait_task = asyncio.create_task(process.wait())
    try:
        stdout_result, stderr_result, exit_code = await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task, wait_task),
            timeout=config.timeout_ms / 1000,
        )
    except TimeoutError:
        process_usage = (await owner.terminate(process)).to_dict()
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return HookProcessResult(
            "timeout", None, "", "", False, process_usage
        )
    process_usage = owner.forget(process).to_dict()
    stdout_bytes, stdout_truncated = stdout_result
    stderr_bytes, stderr_truncated = stderr_result
    return HookProcessResult(
        "completed" if exit_code == 0 else "failed",
        exit_code,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated or stderr_truncated,
        process_usage,
    )

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from code_rook.core.hooks.models import HookConfig, HookPayload


@dataclass(frozen=True)
class HookProcessResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    output_truncated: bool


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


# 超时时终止 hook 的整个进程树，并等待根进程退出
async def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            kill_process_group = cast(Callable[[int, int], None], getattr(os, "killpg"))
            kill_process_group(process.pid, int(getattr(signal, "SIGKILL", 9)))
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


# 执行单个进程 hook，并对超时、输出和进程树实施硬边界
async def execute_hook_process(
    config: HookConfig,
    payload: HookPayload,
    *,
    cwd: Path,
) -> HookProcessResult:
    creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    process = await asyncio.create_subprocess_exec(
        *config.command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=creationflags if os.name == "nt" else 0,
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
        await _kill_process_tree(process)
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        return HookProcessResult("timeout", None, "", "", False)
    stdout_bytes, stdout_truncated = stdout_result
    stderr_bytes, stderr_truncated = stderr_result
    return HookProcessResult(
        "completed" if exit_code == 0 else "failed",
        exit_code,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        stdout_truncated or stderr_truncated,
    )

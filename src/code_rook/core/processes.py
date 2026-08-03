from __future__ import annotations

import asyncio
import locale
import os
import signal
import subprocess
from pathlib import Path

_MAX_SHELL_OUTPUT_BYTES = 64 * 1024


# 优先按 UTF-8 解码，Windows 原生命令输出则回退到当前 OEM 代码页
def _decode_shell_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        if os.name == "nt":
            try:
                return data.decode("oem")
            except (LookupError, UnicodeDecodeError):
                pass
        return data.decode(locale.getpreferredencoding(False), errors="replace")


# 创建使用当前平台本地 shell 的子进程，供前台与后台生命周期共享
async def create_shell_process(
    command: str,
    cwd: Path | None = None,
    *,
    interactive_stdin: bool = False,
) -> asyncio.subprocess.Process:
    stdin = asyncio.subprocess.PIPE if interactive_stdin else None
    if os.name == "nt":
        return await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    return await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )


# 把 shell 原始输出转换为有界文本并标记是否截断
def bounded_shell_output(stdout_bytes: bytes) -> tuple[str, bool]:
    truncated = len(stdout_bytes) > _MAX_SHELL_OUTPUT_BYTES
    output = _decode_shell_output(stdout_bytes[:_MAX_SHELL_OUTPUT_BYTES])
    if truncated:
        output += "\n[truncated]"
    return output, truncated


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 1.0,
) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        await _terminate_windows_tree(process, grace_seconds)
    else:
        await _terminate_posix_tree(process, grace_seconds)


async def _terminate_windows_tree(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), timeout=grace_seconds)
    except (FileNotFoundError, OSError, TimeoutError):
        if process.returncode is None:
            process.kill()
    await _wait_or_kill(process, grace_seconds)


async def _terminate_posix_tree(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        process.terminate()
        await _wait_or_kill(process, grace_seconds)
        return
    try:
        kill_group(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        try:
            kill_group(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            pass
        await process.wait()


async def _wait_or_kill(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()

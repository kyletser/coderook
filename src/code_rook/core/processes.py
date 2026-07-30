from __future__ import annotations

import asyncio
import os
import signal


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

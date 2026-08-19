from __future__ import annotations

import asyncio
import locale
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_rook.core.windows_job import WindowsJobObject, create_kill_on_close_job

_MAX_SHELL_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    label: str
    kind: str
    job_managed: bool = False


@dataclass(frozen=True)
class ProcessUsage:
    wall_time_ms: int
    user_cpu_ms: int = 0
    system_cpu_ms: int = 0
    peak_memory_bytes: int = 0
    process_count: int = 1
    samples: int = 0
    complete: bool = False

    # 返回可直接写入类型化事件和 receipt 的稳定字典
    def to_dict(self) -> dict[str, object]:
        return {
            "wall_time_ms": self.wall_time_ms,
            "user_cpu_ms": self.user_cpu_ms,
            "system_cpu_ms": self.system_cpu_ms,
            "peak_memory_bytes": self.peak_memory_bytes,
            "process_count": self.process_count,
            "samples": self.samples,
            "complete": self.complete,
        }


@dataclass
class _ProcessUsageState:
    started: float
    user_cpu_ms: int = 0
    system_cpu_ms: int = 0
    peak_memory_bytes: int = 0
    process_count: int = 1
    samples: int = 0
    complete: bool = False


class ProcessSupervisor:
    # 初始化 daemon 级子进程登记表，所有记录仅在当前事件循环内修改
    def __init__(self) -> None:
        self._processes: dict[int, tuple[asyncio.subprocess.Process, ProcessRecord]] = {}
        self._windows_jobs: dict[int, WindowsJobObject] = {}
        self._usage: dict[int, _ProcessUsageState] = {}
        self._monitors: dict[int, asyncio.Task[None]] = {}

    # 使用统一进程组边界启动 argv 子进程并登记用途
    async def start_exec(
        self,
        *argv: str,
        label: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stdin: Any = None,
        stdout: Any = None,
        stderr: Any = None,
        limit: int | None = None,
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "env": env,
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
        }
        if limit is not None:
            kwargs["limit"] = limit
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        await self._attach_windows_job(process)
        self._processes[process.pid] = (
            process,
            ProcessRecord(
                pid=process.pid,
                label=label,
                kind="exec",
                job_managed=process.pid in self._windows_jobs,
            ),
        )
        self._begin_usage_monitor(process)
        return process

    # 启动本地 shell 命令并登记为受管进程
    async def start_shell(
        self,
        command: str,
        *,
        label: str,
        cwd: Path | None = None,
        interactive_stdin: bool = False,
    ) -> asyncio.subprocess.Process:
        process = await create_shell_process(
            command,
            cwd,
            interactive_stdin=interactive_stdin,
        )
        await self._attach_windows_job(process)
        self._processes[process.pid] = (
            process,
            ProcessRecord(
                pid=process.pid,
                label=label,
                kind="shell",
                job_managed=process.pid in self._windows_jobs,
            ),
        )
        self._begin_usage_monitor(process)
        return process

    # 初始化进程资源状态，并在 Linux 上周期采样整个进程组
    def _begin_usage_monitor(self, process: asyncio.subprocess.Process) -> None:
        self._usage[process.pid] = _ProcessUsageState(started=time.monotonic())
        if os.name == "posix" and Path("/proc").is_dir():
            self._monitors[process.pid] = asyncio.create_task(
                self._monitor_linux_group(process),
                name=f"process-usage:{process.pid}",
            )

    # 周期读取 Linux /proc 的同进程组 CPU 与 RSS，保留生命周期峰值
    async def _monitor_linux_group(self, process: asyncio.subprocess.Process) -> None:
        while process.returncode is None:
            sample = await asyncio.to_thread(_linux_process_group_usage, process.pid)
            state = self._usage.get(process.pid)
            if state is None:
                return
            if sample is not None:
                user_ms, system_ms, memory_bytes, process_count = sample
                state.user_cpu_ms = max(state.user_cpu_ms, user_ms)
                state.system_cpu_ms = max(state.system_cpu_ms, system_ms)
                state.peak_memory_bytes = max(state.peak_memory_bytes, memory_bytes)
                state.process_count = max(state.process_count, process_count)
                state.samples += 1
                state.complete = True
            await asyncio.sleep(0.05)

    # 读取当前进程或 Job 的最新资源用量，不改变监管生命周期
    def usage(self, process: asyncio.subprocess.Process) -> ProcessUsage:
        state = self._usage.get(process.pid)
        if state is None:
            return ProcessUsage(wall_time_ms=0, complete=False)
        job = self._windows_jobs.get(process.pid)
        if job is not None:
            measured = job.usage()
            if measured:
                state.user_cpu_ms = int(measured["user_cpu_ms"])
                state.system_cpu_ms = int(measured["system_cpu_ms"])
                state.peak_memory_bytes = max(
                    state.peak_memory_bytes,
                    int(measured["peak_memory_bytes"]),
                )
                state.process_count = max(
                    state.process_count,
                    int(measured["process_count"]),
                )
                state.samples += 1
                state.complete = True
        return ProcessUsage(
            wall_time_ms=max(0, int((time.monotonic() - state.started) * 1000)),
            user_cpu_ms=state.user_cpu_ms,
            system_cpu_ms=state.system_cpu_ms,
            peak_memory_bytes=state.peak_memory_bytes,
            process_count=state.process_count,
            samples=state.samples,
            complete=state.complete,
        )

    # Windows 上把根进程加入 kill-on-close Job Object，失败则终止未受管进程并拒绝启动
    async def _attach_windows_job(self, process: asyncio.subprocess.Process) -> None:
        if os.name != "nt":
            return
        try:
            job = await asyncio.to_thread(create_kill_on_close_job, process.pid)
        except OSError as exc:
            await terminate_process_tree(process)
            raise RuntimeError(
                f"failed to attach process {process.pid} to Windows Job Object: {exc}"
            ) from exc
        self._windows_jobs[process.pid] = job

    # 标记已自然退出且已回收的进程，避免 shutdown 再次处理
    def forget(self, process: asyncio.subprocess.Process) -> ProcessUsage:
        usage = self.usage(process)
        self._processes.pop(process.pid, None)
        monitor = self._monitors.pop(process.pid, None)
        if monitor is not None:
            monitor.cancel()
        job = self._windows_jobs.pop(process.pid, None)
        if job is not None:
            job.close()
        self._usage.pop(process.pid, None)
        return usage

    # 终止指定进程树并从登记表移除
    async def terminate(self, process: asyncio.subprocess.Process) -> ProcessUsage:
        try:
            job = self._windows_jobs.get(process.pid)
            if job is not None:
                await asyncio.to_thread(job.terminate)
                await _wait_or_kill(process, 1.0)
            else:
                await terminate_process_tree(process)
        finally:
            usage = self.forget(process)
        return usage

    # 返回当前登记进程的稳定快照，供诊断和测试使用
    def snapshot(self) -> tuple[ProcessRecord, ...]:
        return tuple(record for _process, record in self._processes.values())

    # 按 pid 顺序终止 daemon 仍持有的全部子进程树
    async def close(self) -> None:
        entries = sorted(self._processes.values(), key=lambda item: item[0].pid)
        for process, _record in entries:
            await self.terminate(process)
        self._processes.clear()
        self._windows_jobs.clear()
        self._usage.clear()
        self._monitors.clear()


# 汇总 Linux 同进程组的 CPU tick、当前 RSS 和进程数，读取竞争失败时跳过该样本
def _linux_process_group_usage(pid: int) -> tuple[int, int, int, int] | None:
    try:
        sysconf = getattr(os, "sysconf")
        clock_ticks = int(sysconf("SC_CLK_TCK"))
        page_size = int(sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        return None
    user_ticks = 0
    system_ticks = 0
    rss_pages = 0
    process_count = 0
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text(encoding="utf-8")
            close_paren = raw.rfind(")")
            if close_paren < 0:
                continue
            fields = raw[close_paren + 2 :].split()
            if len(fields) <= 21 or int(fields[2]) != pid:
                continue
            user_ticks += int(fields[11])
            system_ticks += int(fields[12])
            rss_pages += max(0, int(fields[21]))
            process_count += 1
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    if process_count == 0:
        return None
    return (
        int(user_ticks * 1000 / clock_ticks),
        int(system_ticks * 1000 / clock_ticks),
        rss_pages * page_size,
        process_count,
    )


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

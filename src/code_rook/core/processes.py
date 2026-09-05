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
_SHELL_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "COLORTERM",
        "SHELL",
        "USER",
        "LOGNAME",
        "VIRTUAL_ENV",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONDONTWRITEBYTECODE",
        "NO_COLOR",
        "FORCE_COLOR",
    }
)
_SENSITIVE_ENV_FRAGMENTS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
    "AUTH_SOCK",
    "GITHUB_",
    "GITLAB_",
    "AWS_",
    "AZURE_",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "SSH_",
)


# 从 daemon 环境构造最小 shell 白名单并无条件移除凭据类变量
def sanitized_shell_environment(
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    source = dict(os.environ)
    if overrides:
        source.update(overrides)
    result: dict[str, str] = {}
    for key, value in source.items():
        upper = key.upper()
        if upper not in _SHELL_ENV_ALLOWLIST:
            continue
        if any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS):
            continue
        result[key] = value
    return result


# 在脱敏基础环境上仅加入用户显式交给扩展进程的变量
def explicit_extension_environment(
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    result = sanitized_shell_environment()
    if overrides:
        result.update(overrides)
    return result


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    label: str
    kind: str
    job_managed: bool = False
    process_group_id: int | None = None


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
        self._posix_groups: dict[int, int] = {}
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
            "env": sanitized_shell_environment() if env is None else env,
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
        process_group_id = _capture_posix_process_group(process)
        if process_group_id is not None:
            self._posix_groups[process.pid] = process_group_id
        self._processes[process.pid] = (
            process,
            ProcessRecord(
                pid=process.pid,
                label=label,
                kind="exec",
                job_managed=process.pid in self._windows_jobs,
                process_group_id=process_group_id,
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
        env: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> asyncio.subprocess.Process:
        process = await create_shell_process(
            command,
            cwd,
            interactive_stdin=interactive_stdin,
            env=env,
            limit=limit,
        )
        await self._attach_windows_job(process)
        process_group_id = _capture_posix_process_group(process)
        if process_group_id is not None:
            self._posix_groups[process.pid] = process_group_id
        self._processes[process.pid] = (
            process,
            ProcessRecord(
                pid=process.pid,
                label=label,
                kind="shell",
                job_managed=process.pid in self._windows_jobs,
                process_group_id=process_group_id,
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

    # Windows 上把活跃根进程加入 Job；已在分配竞态中自然退出的短命进程可安全返回
    async def _attach_windows_job(self, process: asyncio.subprocess.Process) -> None:
        if os.name != "nt":
            return
        try:
            job = await asyncio.to_thread(create_kill_on_close_job, process.pid)
        except OSError as exc:
            try:
                await asyncio.wait_for(process.wait(), timeout=0.05)
            except TimeoutError:
                await terminate_process_tree(process)
                raise RuntimeError(
                    f"failed to attach process {process.pid} to Windows Job Object: {exc}"
                ) from exc
            return
        self._windows_jobs[process.pid] = job

    # 回收已退出根进程，并在移除记录前强制清理 POSIX 残留进程组
    def forget(self, process: asyncio.subprocess.Process) -> ProcessUsage:
        usage = self.usage(process)
        process_group_id = self._posix_groups.get(process.pid)
        if process.returncode is not None and process_group_id is not None:
            _terminate_residual_posix_group(process_group_id)
        self._processes.pop(process.pid, None)
        self._posix_groups.pop(process.pid, None)
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
                await terminate_process_tree(
                    process,
                    process_group_id=self._posix_groups.get(process.pid),
                )
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
        self._posix_groups.clear()
        self._usage.clear()
        self._monitors.clear()


# 在 POSIX 启动窗口内捕获新会话的真实进程组号，leader 过早退出时退回其 PID
def _capture_posix_process_group(
    process: asyncio.subprocess.Process,
) -> int | None:
    if os.name != "posix":
        return None
    get_process_group = getattr(os, "getpgid", None)
    if get_process_group is None:
        return process.pid
    try:
        return int(get_process_group(process.pid))
    except (OSError, ProcessLookupError):
        return process.pid


# 探测指定 POSIX 进程组是否仍有成员，权限拒绝视为仍存活以避免误判安全清理完成
def _posix_process_group_exists(process_group_id: int) -> bool:
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        return False
    try:
        kill_group(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    linux_live_members = _linux_process_group_has_live_members(process_group_id)
    if linux_live_members is not None:
        return linux_live_members
    return True


# 在 Linux /proc 中区分可运行成员与已终止僵尸，其他 POSIX 平台返回未知
def _linux_process_group_has_live_members(
    process_group_id: int,
) -> bool | None:
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return None
    matched = False
    for stat_path in proc_root.glob("[0-9]*/stat"):
        try:
            raw = stat_path.read_text(encoding="utf-8")
            close_paren = raw.rfind(")")
            if close_paren < 0:
                continue
            fields = raw[close_paren + 2 :].split()
            if len(fields) <= 2 or int(fields[2]) != process_group_id:
                continue
            matched = True
            if fields[0] != "Z":
                return True
        except (OSError, UnicodeDecodeError, ValueError):
            continue
    return False if matched else None


# 在同步 forget 路径强制清除已退出 leader 遗留的组成员，防止记录移除后永久失联
def _terminate_residual_posix_group(process_group_id: int) -> None:
    if not _posix_process_group_exists(process_group_id):
        return
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        return
    try:
        kill_group(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        kill_group(
            process_group_id,
            getattr(signal, "SIGKILL", signal.SIGTERM),
        )
    except ProcessLookupError:
        pass


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
    env: dict[str, str] | None = None,
    limit: int | None = None,
) -> asyncio.subprocess.Process:
    stdin = asyncio.subprocess.PIPE if interactive_stdin else None
    process_env = sanitized_shell_environment(env)
    stream_options: dict[str, Any] = {}
    if limit is not None:
        stream_options["limit"] = limit
    if os.name == "nt":
        return await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=process_env,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            **stream_options,
        )
    return await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        stdin=stdin,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=process_env,
        start_new_session=True,
        **stream_options,
    )


# 把 shell 原始输出转换为有界文本并标记是否截断
def bounded_shell_output(stdout_bytes: bytes) -> tuple[str, bool]:
    truncated = len(stdout_bytes) > _MAX_SHELL_OUTPUT_BYTES
    output = _decode_shell_output(stdout_bytes[:_MAX_SHELL_OUTPUT_BYTES])
    if truncated:
        output += "\n[truncated]"
    return output, truncated


# 只等待根进程退出而不等待其后代关闭继承的 stdout/stderr 管道
async def wait_for_process_leader(
    process: asyncio.subprocess.Process,
) -> int:
    while process.returncode is None:
        await asyncio.sleep(0.01)
    return process.returncode


# 终止新会话进程的完整平台进程树，并在 POSIX 上处理已退出 leader 的残留组
async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 1.0,
    process_group_id: int | None = None,
) -> None:
    if os.name == "nt":
        if process.returncode is not None:
            return
        await _terminate_windows_tree(process, grace_seconds)
    else:
        await _terminate_posix_tree(
            process,
            grace_seconds,
            process_group_id=process_group_id,
        )


# 通过 Job 外的 taskkill 兼容路径终止 Windows 根进程及其后代
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


# 按已保存 PGID 终止 POSIX 进程组，leader 先退出时仍继续处理组成员
async def _terminate_posix_tree(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
    *,
    process_group_id: int | None = None,
) -> None:
    kill_group = getattr(os, "killpg", None)
    if kill_group is None:
        if process.returncode is not None:
            return
        process.terminate()
        await _wait_or_kill(process, grace_seconds)
        return
    group_id = process_group_id or process.pid
    try:
        kill_group(group_id, signal.SIGTERM)
    except ProcessLookupError:
        if process.returncode is None:
            await _wait_or_kill(process, grace_seconds)
        return
    group_exited = await _wait_for_posix_group_exit(group_id, grace_seconds)
    if not group_exited:
        try:
            kill_group(group_id, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            pass
        await _wait_for_posix_group_exit(group_id, grace_seconds)
    if process.returncode is None:
        await _wait_or_kill(process, grace_seconds)


# 有界轮询 POSIX 进程组是否已经消失，使 leader 提前退出时仍等待其后台后代
async def _wait_for_posix_group_exit(
    process_group_id: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _posix_process_group_exists(process_group_id):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.02, remaining))
    return True


# 等待根进程退出并在宽限期后强制终止，供无进程组能力的平台复用
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

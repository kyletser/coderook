from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient

_PID_FILE = Path.home() / ".coderook" / "coderook-core.pid"


class CoreLaunchError(RuntimeError):
    pass


# 判断 Core 是否已完成认证并能响应 ping；未启动或 token 尚未生成时返回 False
def _core_ready(config: CodeRookConfig) -> bool:
    try:
        asyncio.run(_ping_check(config))
        return True
    except (ConnectionRefusedError, OSError, IpcTokenError):
        return False
    except IpcError as exc:
        raise CoreLaunchError(f"Core authentication failed: {exc}") from exc


# 启动后台 Core 并记录 PID，返回进程对象供就绪等待与失败检测
def _spawn_core() -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "code_rook.core"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return proc


# 确保 Core 可用；已有实例直接复用，否则后台启动并等待认证就绪
def ensure_core_running(config: CodeRookConfig, timeout_s: float = 10.0) -> bool:
    if _core_ready(config):
        return False

    port_open = asyncio.run(_port_open(config))
    proc = None if port_open else _spawn_core()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            _PID_FILE.unlink(missing_ok=True)
            raise CoreLaunchError(
                f"Core exited during startup with code {proc.returncode}; "
                "check .env or run `uv run coderook-core` for details"
            )
        if _core_ready(config):
            return proc is not None
        time.sleep(0.05)

    raise CoreLaunchError(
        f"Core did not become ready at {config.host}:{config.port} within {timeout_s:g}s"
    )


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return False
        kernel32 = windll.kernel32
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        get_last_error = getattr(ctypes, "get_last_error", lambda: 0)
        return int(get_last_error()) == 5  # access denied still means the PID exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: CodeRookConfig) -> None:
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        await asyncio.wait_for(
            client.send_command("core.ping", {"client": "cli/core-check"}),
            timeout=2.0,
        )
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


async def _port_open(config: CodeRookConfig) -> bool:
    try:
        _reader, writer = await asyncio.open_connection(config.host, config.port)
    except (ConnectionRefusedError, OSError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


# 读取 PID 文件并确认进程存活，进程已消失则删除文件并返回 None
def _running_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        if not _pid_exists(pid):
            _PID_FILE.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None


# 停止由 CodeRook 启动并记录 PID 的 Core，等待进程退出后返回是否执行了停止
def stop_core(timeout_s: float = 5.0) -> bool:
    pid = _running_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    _PID_FILE.unlink(missing_ok=True)
    return True


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: CodeRookConfig) -> None:
    try:
        asyncio.run(_ping_check(config))
        print(f"running  ({config.host}:{config.port})")
    except (ConnectionRefusedError, OSError):
        print("not running")
    except IpcTokenError as exc:
        state = "running (token unavailable)" if asyncio.run(_port_open(config)) else "not running"
        print(f"{state}  {exc}")
    except IpcError as exc:
        print(f"running (authentication failed: {exc})")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: CodeRookConfig) -> None:
    try:
        started = ensure_core_running(config)
    except CoreLaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return

    if started:
        pid = _running_pid()
        print(f"started  pid={pid}  ({config.host}:{config.port})")
    else:
        print(f"already running  ({config.host}:{config.port})")


# 向 daemon 发送 SIGTERM 停止进程，若未运行则提示
def cmd_core_stop(config: CodeRookConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    stop_core()
    print(f"stopped  pid={pid}")


# 重启后台 Core，使磁盘上的最新配置立即生效
def cmd_core_restart(config: CodeRookConfig) -> None:
    stopped = stop_core()
    try:
        started = ensure_core_running(config)
    except CoreLaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return
    action = "restarted" if stopped else ("started" if started else "already running")
    print(f"{action}  pid={_running_pid()}  ({config.host}:{config.port})")

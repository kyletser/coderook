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


# 读取已认证 Core 的运行元数据；未启动或 token 尚未生成时返回 None
def _core_metadata(config: CodeRookConfig) -> dict[str, object] | None:
    try:
        return asyncio.run(_ping_check(config))
    except (ConnectionRefusedError, OSError, IpcTokenError):
        return None
    except IpcError as exc:
        raise CoreLaunchError(f"Core authentication failed: {exc}") from exc


# 判断 Core 是否已完成认证并能响应 ping
def _core_ready(config: CodeRookConfig) -> bool:
    return _core_metadata(config) is not None


# 使用平台一致的大小写与绝对路径规则比较两个 workspace
def _same_workspace(left: str | Path, right: str | Path) -> bool:
    left_path = os.path.normcase(str(Path(left).resolve()))
    right_path = os.path.normcase(str(Path(right).resolve()))
    return left_path == right_path


# 从松散 IPC 元数据安全读取非负整数，异常值按零处理
def _metadata_count(metadata: dict[str, object], key: str) -> int:
    value = metadata.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, str)):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


# 校验手动 Core 的 workspace；显式 env overlay 无可验身份时失败关闭
def validate_core_workspace(
    config: CodeRookConfig,
    *,
    env_file: Path | None = None,
) -> None:
    requested_workspace = Path.cwd().resolve()
    metadata = _core_metadata(config)
    if metadata is None:
        raise CoreLaunchError(
            "Core is not running; start it in this workspace or remove --no-auto-core"
        )
    served_workspace = str(metadata.get("workspace") or "")
    if not served_workspace:
        raise CoreLaunchError(
            "Core did not report its workspace; restart it before connecting manually"
        )
    if not _same_workspace(served_workspace, requested_workspace):
        raise CoreLaunchError(
            "Core is serving another workspace: "
            f"{served_workspace}; restart it from {requested_workspace}"
        )
    if env_file is not None:
        raise CoreLaunchError(
            "Cannot verify that the manually managed Core was started with the "
            "same explicit env file; remove --no-auto-core so CodeRook can restart "
            "its managed Core, or omit --env-file"
        )


# 启动后台 Core 并显式转发用户选择的环境文件，返回进程对象供就绪等待
def _spawn_core(env_file: Path | None = None) -> subprocess.Popen[bytes]:
    command = [sys.executable, "-m", "code_rook.core"]
    if env_file is not None:
        command.extend(["--env-file", str(env_file.expanduser().resolve())])
    proc = subprocess.Popen(
        command,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    return proc


# 确保 Core 可用；显式 env 文件会强制受管实例重启以冻结同一 overlay
def ensure_core_running(
    config: CodeRookConfig,
    timeout_s: float = 10.0,
    *,
    env_file: Path | None = None,
) -> bool:
    requested_workspace = Path.cwd().resolve()
    metadata = _core_metadata(config)
    restarted_existing = False
    if metadata is not None:
        served_workspace = str(metadata.get("workspace") or "")
        same_workspace = bool(served_workspace) and _same_workspace(
            served_workspace,
            requested_workspace,
        )
        if same_workspace and env_file is None:
            return False
        active_runs = _metadata_count(metadata, "active_runs")
        if active_runs:
            scope = "this workspace" if same_workspace else "another workspace"
            raise CoreLaunchError(
                f"Core is busy in {scope}: "
                f"{served_workspace or '<unknown>'} ({active_runs} active run(s)); "
                "finish or cancel that work before restarting Core"
            )
        if _running_pid() is None:
            reason = (
                "an explicit env file requires a verified restart"
                if same_workspace and env_file is not None
                else "the requested workspace differs"
            )
            raise CoreLaunchError(
                "Core was not started by this CLI and cannot be safely reused because "
                f"{reason}: {served_workspace or '<unknown>'}; stop it manually, then retry"
            )
        if not stop_core(config):
            raise CoreLaunchError(
                f"Could not stop the Core serving {served_workspace or '<unknown>'}"
            )
        restarted_existing = True

    port_open = asyncio.run(_port_open(config))
    if restarted_existing and port_open:
        raise CoreLaunchError(
            "Core still owns the configured port after the required restart; "
            "refusing to reuse an instance whose explicit env overlay cannot be verified"
        )
    if env_file is not None and port_open:
        raise CoreLaunchError(
            "An unverified Core already owns the configured port; refusing to reuse it "
            "with --env-file because its credential overlay cannot be authenticated"
        )
    proc = None
    if not port_open:
        proc = _spawn_core() if env_file is None else _spawn_core(env_file)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            _PID_FILE.unlink(missing_ok=True)
            raise CoreLaunchError(
                f"Core exited during startup with code {proc.returncode}; "
                "run `uv run coderook-core` in the workspace for details"
            )
        ready = _core_metadata(config)
        if ready is not None:
            served_workspace = str(ready.get("workspace") or "")
            if served_workspace and not _same_workspace(
                served_workspace,
                requested_workspace,
            ):
                raise CoreLaunchError(
                    f"Core started in unexpected workspace: {served_workspace}"
                )
            return proc is not None
        time.sleep(0.05)

    raise CoreLaunchError(
        f"Core did not become ready at {config.host}:{config.port} within {timeout_s:g}s"
    )


# 判断指定 PID 的进程是否仍在运行（Windows 用 OpenProcess，access denied 也算存活）
def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        windll = getattr(ctypes, "windll", None)
        win_dll = getattr(ctypes, "WinDLL", None)
        if windll is None or win_dll is None:
            return False
        # 必须以 use_last_error=True 创建：windll 共享的 ctypes 私有错误副本不会
        # 被 kernel32 调用填充，get_last_error() 恒为 0，会把活的提权 daemon 误判为已退出
        kernel32 = win_dll("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        get_last_error = getattr(ctypes, "get_last_error", None)
        if get_last_error is None:
            return False
        return int(get_last_error()) == 5  # access denied still means the PID exists
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# 尝试连接 daemon，成功则正常返回，失败则抛出 ConnectionRefusedError/OSError
async def _ping_check(config: CodeRookConfig) -> dict[str, object]:
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        result = await asyncio.wait_for(
            client.send_command("core.ping", {"client": "cli/core-check"}),
            timeout=2.0,
        )
        return dict(result)
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


# 经 IPC 请求 daemon 有序关闭；连接或认证失败时抛出由调用方降级处理
async def _request_shutdown(config: CodeRookConfig) -> None:
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        await asyncio.wait_for(
            client.send_command("core.shutdown", {"reason": "cli stop"}),
            timeout=3.0,
        )
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


# 停止 Core：优先 IPC 优雅关闭，失败回退 SIGTERM；等待进程退出后返回是否执行了停止
def stop_core(config: CodeRookConfig | None = None, timeout_s: float = 5.0) -> bool:
    pid = _running_pid()
    if pid is None:
        return False
    graceful = False
    if config is not None:
        try:
            asyncio.run(_request_shutdown(config))
            graceful = True
        except (ConnectionRefusedError, OSError, IpcTokenError, IpcError, TimeoutError):
            graceful = False
    if not graceful:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_s
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if graceful and _pid_exists(pid):
        # 优雅关闭超时仍未退出，回退强制终止以避免残留进程
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_s
        while _pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
    _PID_FILE.unlink(missing_ok=True)
    return True


# 打印 daemon 当前状态（running / not running）
def cmd_core_status(config: CodeRookConfig) -> None:
    try:
        metadata = asyncio.run(_ping_check(config))
        workspace = str(metadata.get("workspace") or "unknown workspace")
        active_runs = _metadata_count(metadata, "active_runs")
        print(
            f"running  ({config.host}:{config.port})  workspace={workspace}  "
            f"active_runs={active_runs}"
        )
    except (ConnectionRefusedError, OSError):
        print("not running")
    except IpcTokenError as exc:
        state = "running (token unavailable)" if asyncio.run(_port_open(config)) else "not running"
        print(f"{state}  {exc}")
    except IpcError as exc:
        print(f"running (authentication failed: {exc})")


# 在后台启动 daemon，若已在运行则提示并退出
def cmd_core_start(config: CodeRookConfig, *, env_file: Path | None = None) -> None:
    try:
        started = ensure_core_running(config, env_file=env_file)
    except CoreLaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        # 启动失败必须以非零码退出，否则包装 CLI 的脚本无法感知 daemon 未启动
        raise SystemExit(1) from exc

    if started:
        pid = _running_pid()
        print(f"started  pid={pid}  ({config.host}:{config.port})")
    else:
        print(f"already running  ({config.host}:{config.port})")


# 优先经 IPC 有序停止 daemon，未运行时提示
def cmd_core_stop(config: CodeRookConfig) -> None:
    pid = _running_pid()
    if pid is None:
        print("not running")
        return
    stop_core(config)
    print(f"stopped  pid={pid}")


# 重启后台 Core，使磁盘上的最新配置立即生效
def cmd_core_restart(config: CodeRookConfig, *, env_file: Path | None = None) -> None:
    stopped = stop_core(config)
    try:
        started = ensure_core_running(config, env_file=env_file)
    except CoreLaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        # 与 start 保持一致：重启失败同样必须以非零码退出
        raise SystemExit(1) from exc
    action = "restarted" if stopped else ("started" if started else "already running")
    print(f"{action}  pid={_running_pid()}  ({config.host}:{config.port})")

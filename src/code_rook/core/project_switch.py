from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


# 判断目标进程是否仍然存活
def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return False
        kernel32 = win_dll("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        get_last_error = getattr(ctypes, "get_last_error", None)
        return get_last_error is not None and int(get_last_error()) == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# 等待旧 Core 退出并在超时后受控终止，再从目标项目目录启动新 Core
def switch_project(workspace: Path, old_pid: int, timeout_seconds: float = 8.0) -> int:
    target = workspace.expanduser().resolve(strict=True)
    if not target.is_dir():
        return 2
    deadline = time.monotonic() + timeout_seconds
    while _pid_exists(old_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_exists(old_pid):
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass
        forced_deadline = time.monotonic() + 5.0
        while _pid_exists(old_pid) and time.monotonic() < forced_deadline:
            time.sleep(0.05)
    if _pid_exists(old_pid):
        return 3
    process = subprocess.Popen(
        [sys.executable, "-m", "code_rook.core"],
        cwd=target,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = Path.home() / ".coderook" / "coderook-core.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(process.pid), encoding="utf-8")
    return 0


# 解析内部项目切换参数并执行一次交接
def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--old-pid", type=int, required=True)
    args = parser.parse_args()
    return switch_project(args.workspace, args.old_pid)


if __name__ == "__main__":
    raise SystemExit(main())

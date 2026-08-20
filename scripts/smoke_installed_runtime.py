#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


# 向操作系统申请一个临时可用的 loopback 端口
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# 构造不继承开发机凭据和状态的已安装运行时环境
def first_run_environment(home: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("CODEROOK_")
        and not key.upper().endswith(_CREDENTIAL_SUFFIXES)
        and key.upper() != "PYTHONPATH"
    }
    core_port = _free_port()
    api_port = _free_port()
    while api_port == core_port:
        api_port = _free_port()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONUTF8": "1",
            "CODEROOK_HOST": "127.0.0.1",
            "CODEROOK_PORT": str(core_port),
            "CODEROOK_API_HOST": "127.0.0.1",
            "CODEROOK_API_PORT": str(api_port),
            "CODEROOK_IPC_TOKEN": secrets.token_urlsafe(32),
            "CODEROOK_LOG_FILE": "",
            "CODEROOK_LOG_LEVEL": "WARNING",
            "CODEROOK_TRACE_ENABLED": "false",
        }
    )
    return env


# 用当前已安装 Python 执行控制台入口并返回脱敏文本输出
def _run_entrypoint(
    import_statement: str,
    args: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", import_statement, *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )


# 等待已安装 Core 开始监听或提前报告子进程失败
async def _wait_until_listening(
    port: int,
    process: subprocess.Popen[str],
) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"installed Core exited with {process.returncode}\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    raise TimeoutError("installed Core did not listen within 10 seconds")


# 验证已安装 runtime 的版本、TUI help、零凭据状态与真实 daemon ping
def smoke() -> None:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="coderook-installed-smoke-",
        ignore_cleanup_errors=True,
    ) as raw_temp:
        root = Path(raw_temp)
        home = root / "home"
        home.mkdir()
        env = first_run_environment(home)

        version = _run_entrypoint(
            "from code_rook.cli.main import main; main()",
            ["--version"],
            env=env,
            cwd=root,
        )
        if re.fullmatch(
            r"\d+\.\d+\.\d+(?:[.-][0-9A-Za-z]+)*",
            version.stdout.strip(),
        ) is None:
            raise RuntimeError(f"unexpected version output: {version.stdout!r}")
        _run_entrypoint(
            "from code_rook.tui.__main__ import main; main()",
            ["--help"],
            env=env,
            cwd=root,
        )
        status = _run_entrypoint(
            "from code_rook.cli.main import main; main()",
            ["config-status"],
            env=env,
            cwd=root,
        )
        if "status:   incomplete" not in status.stdout:
            raise RuntimeError(f"unexpected first-run status: {status.stdout!r}")

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from code_rook.core.app import run; run()",
            ],
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            asyncio.run(_wait_until_listening(int(env["CODEROOK_PORT"]), process))
            ping = _run_entrypoint(
                "from code_rook.cli.main import main; main()",
                ["ping"],
                env=env,
                cwd=root,
            )
            if not ping.stdout.startswith("pong server="):
                raise RuntimeError(f"unexpected ping output: {ping.stdout!r}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
    print(
        "installed runtime smoke passed: "
        f"python={sys.version_info.major}.{sys.version_info.minor} "
        f"elapsed={time.monotonic() - started:.2f}s"
    )


# 执行已安装 runtime smoke 并返回进程状态
def main() -> int:
    smoke()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

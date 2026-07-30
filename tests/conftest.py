from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
def ipc_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = secrets.token_urlsafe(32)
    monkeypatch.setenv("CODEROOK_IPC_TOKEN", token)
    return token


@pytest.fixture
# 创建隔离的 daemon 用户目录，避免集成测试读写开发机状态
def daemon_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
# 启动使用隔离用户目录和随机端口的真实 Core daemon
async def running_daemon(
    free_port: int,
    ipc_token: str,
    daemon_home: Path,
) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["CODEROOK_PORT"] = str(free_port)
    env["CODEROOK_LOG_FILE"] = ""
    env["CODEROOK_LOG_LEVEL"] = "WARNING"
    env["HOME"] = str(daemon_home)
    env["USERPROFILE"] = str(daemon_home)
    # IPC 集成测试不调用真实模型，固定占位配置以避免依赖本机 .env 或 CI Secret
    env["CODEROOK_LLM_PROVIDER"] = "anthropic"
    env["CODEROOK_LLM_DEFAULT_MODEL"] = "claude-test"
    env["CODEROOK_LLM_BASE_URL"] = ""
    env["CODEROOK_LLM_API_KEY_ENV"] = "ANTHROPIC_API_KEY"
    env["ANTHROPIC_API_KEY"] = "test-only-not-a-real-key"

    proc = subprocess.Popen([sys.executable, "-m", "code_rook.core"], env=env)

    # Core startup may cross three seconds on a cold Windows filesystem or while
    # endpoint protection scans a newly spawned interpreter.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        return_code = proc.poll()
        if return_code is not None:
            pytest.fail(f"Daemon exited during startup with code {return_code}")
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail("Daemon did not start within 10 seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

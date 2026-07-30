from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_rook.core.runtime.models import (
    ThreadRecord,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)
from code_rook.core.runtime.store import RuntimeStore


# 等待真实 daemon 开始监听随机端口
async def _wait_until_ready(
    process: subprocess.Popen[bytes],
    port: int,
) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"Daemon exited during startup with code {process.returncode}")
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.05)
            continue
        writer.close()
        await writer.wait_closed()
        return
    pytest.fail("Daemon did not start within 10 seconds")


# 发送 JSON-RPC 请求并读取对应响应
async def _send_recv(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict[str, object],
    req_id: str,
) -> dict[str, object]:
    request = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }
    writer.write((json.dumps(request) + "\n").encode())
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=5.0))


# 功能：验证真实 daemon 启动会修复旧 boot 的孤立工具调用，并通过 replay 暴露中断事件
# 设计：启动前预置 running turn，拉起独立 Core 进程后同时检查 SQLite 原子结果和 IPC 事件序列
async def test_daemon_boot_recovers_stale_runtime(
    free_port: int,
    ipc_token: str,
    daemon_home: Path,
) -> None:
    runtime_path = daemon_home / ".coderook" / "runtime.db"
    store = RuntimeStore(runtime_path)
    now = datetime(2026, 7, 30, tzinfo=UTC)
    store.create_thread(
        ThreadRecord(
            id="thread-stale",
            title="stale",
            workspace=str(daemon_home),
            status="running",
            created_at=now,
            updated_at=now,
        )
    )
    store.create_turn(
        TurnRecord(
            id="turn-stale",
            thread_id="thread-stale",
            status=TurnStatus.RUNNING,
            boot_id="boot-old",
            created_at=now,
            updated_at=now,
        )
    )
    store.record_item_and_event(
        TurnItemRecord(
            id="call-stale",
            turn_id="turn-stale",
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="tool-stale",
            payload={"name": "File"},
            created_at=now,
        ),
        event_type="tool.started",
        event_payload={},
        event_ts=now,
    )

    env = os.environ.copy()
    env["CODEROOK_PORT"] = str(free_port)
    env["CODEROOK_LOG_FILE"] = ""
    env["CODEROOK_LOG_LEVEL"] = "WARNING"
    env["CODEROOK_IPC_TOKEN"] = ipc_token
    env["HOME"] = str(daemon_home)
    env["USERPROFILE"] = str(daemon_home)
    env["CODEROOK_LLM_PROVIDER"] = "anthropic"
    env["CODEROOK_LLM_DEFAULT_MODEL"] = "claude-test"
    env["CODEROOK_LLM_BASE_URL"] = ""
    env["CODEROOK_LLM_API_KEY_ENV"] = "ANTHROPIC_API_KEY"
    env["ANTHROPIC_API_KEY"] = "test-only-not-a-real-key"
    process = subprocess.Popen([sys.executable, "-m", "code_rook.core"], env=env)
    writer: asyncio.StreamWriter | None = None
    try:
        await _wait_until_ready(process, free_port)
        reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
        authenticated = await _send_recv(
            reader,
            writer,
            "core.authenticate",
            {"token": ipc_token},
            "auth",
        )
        assert authenticated["result"] == {"authenticated": True}
        replayed = await _send_recv(
            reader,
            writer,
            "event.replay",
            {"thread_id": "thread-stale", "after_seq": 1},
            "replay",
        )

        result = replayed["result"]
        assert isinstance(result, dict)
        assert [event["type"] for event in result["events"]] == ["turn.interrupted"]
        assert store.get_turn("turn-stale").status == TurnStatus.INTERRUPTED
        items = store.list_items("turn-stale")
        assert items[-1].kind == TurnItemKind.TOOL_RESULT
        assert items[-1].payload["reason"] == "daemon_restarted"
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

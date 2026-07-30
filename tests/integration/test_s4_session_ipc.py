from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from code_rook.core.runtime.models import ThreadStatus
from code_rook.core.runtime.store import RuntimeStore


# 发送一条 JSON-RPC 请求并返回响应对象
async def _send_recv(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict,
    req_id: str = "1",
) -> dict:
    req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    writer.write((json.dumps(req) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=5.0)
    return json.loads(line)


async def _authenticate(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    token: str,
) -> None:
    response = await _send_recv(
        reader,
        writer,
        "core.authenticate",
        {"token": token},
        req_id="auth",
    )
    assert response["result"] == {"authenticated": True}


# 功能：验证 daemon 暴露 session create/history/list/close/resume IPC 命令
# 设计：不触发真实 LLM，只验证 CoreApp 协议、状态持久化和恢复入口
async def test_session_create_history_close_over_ipc(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    ipc_token: str,
    daemon_home: Path,
) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
    await _authenticate(reader, writer, ipc_token)

    created = await _send_recv(
        reader,
        writer,
        "session.create",
        {"mode": "chat", "title": "ipc test"},
        req_id="create",
    )
    assert "result" in created, created
    session_id = created["result"]["session_id"]
    assert created["result"]["status"] == "active"

    history = await _send_recv(
        reader,
        writer,
        "session.get_history",
        {"session_id": session_id},
        req_id="history",
    )
    assert history["result"]["messages"] == []

    listed = await _send_recv(
        reader,
        writer,
        "session.list",
        {"include_closed": True},
        req_id="list",
    )
    listed_ids = [item["session_id"] for item in listed["result"]["sessions"]]
    assert session_id in listed_ids

    renamed = await _send_recv(
        reader,
        writer,
        "session.rename",
        {"session_id": session_id, "title": "renamed over IPC"},
        req_id="rename",
    )
    assert renamed["result"]["session"]["title"] == "renamed over IPC"

    forked = await _send_recv(
        reader,
        writer,
        "session.fork",
        {"session_id": session_id, "title": "fork over IPC"},
        req_id="fork",
    )
    fork_id = forked["result"]["session"]["session_id"]
    assert forked["result"]["session"]["parent_session_id"] == session_id
    exported = await _send_recv(
        reader,
        writer,
        "session.export",
        {"session_id": fork_id, "format": "json"},
        req_id="export",
    )
    assert exported["result"]["filename"] == f"{fork_id}.json"
    assert session_id in exported["result"]["content"]
    deleted = await _send_recv(
        reader,
        writer,
        "session.delete",
        {"session_id": fork_id},
        req_id="delete",
    )
    assert deleted["result"] == {"session_id": fork_id, "deleted": True}

    closed = await _send_recv(
        reader,
        writer,
        "session.close",
        {"session_id": session_id},
        req_id="close",
    )
    assert closed["result"]["status"] == "closed"

    resumed = await _send_recv(
        reader,
        writer,
        "session.resume",
        {"session_id": session_id},
        req_id="resume",
    )
    assert resumed["result"]["session"]["status"] == "waiting_for_input"

    inactive_cancel = await _send_recv(
        reader,
        writer,
        "run.cancel",
        {"run_id": "run-does-not-exist"},
        req_id="cancel",
    )
    assert inactive_cancel["error"]["code"] == -32014

    runtime = RuntimeStore(daemon_home / ".coderook" / "runtime.db")
    thread = runtime.get_thread(session_id)
    assert thread.title == "renamed over IPC"
    assert thread.status == ThreadStatus.IDLE
    assert runtime.get_session_facade(session_id).mode == "chat"

    writer.close()
    await writer.wait_closed()

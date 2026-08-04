from __future__ import annotations

import asyncio
import subprocess

import httpx

from code_rook.core.transport.socket_client import SocketClient


# 功能：验证真实 daemon 的 HTTP 与 IPC 客户端读取同一个 durable thread 投影
# 设计：HTTP 创建 thread 后分别经 HTTP list 和 IPC session.list 查询相同 id，覆盖双入口一致性
async def test_http_and_ipc_share_durable_threads(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    api_port: int,
    ipc_token: str,
) -> None:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{api_port}",
        timeout=5.0,
    ) as http:
        created = await http.post(
            "/v1/threads",
            json={"title": "Shared runtime", "mode": "chat"},
        )
        assert created.status_code == 201
        thread_id = created.json()["id"]
        threads = (await http.get("/v1/threads")).json()
        assert any(thread["id"] == thread_id for thread in threads)
        started = await http.post(
            f"/v1/threads/{thread_id}/turns",
            json={"content": "reply briefly", "mode": "act"},
        )
        assert started.status_code == 202
        turn_id = started.json()["id"]
        api_status = started.json()["status"]
        api_usage = started.json()["usage"]

    ipc = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await ipc.connect()
    event_loop = asyncio.create_task(ipc.run_event_loop())
    try:
        result = await asyncio.wait_for(
            ipc.send_command(
                "session.list",
                {"include_closed": True, "limit": 100},
            ),
            timeout=5.0,
        )
        assert any(session["session_id"] == thread_id for session in result["sessions"])
        inspected = await asyncio.wait_for(
            ipc.send_command("turn.inspect", {"turn_id": turn_id}),
            timeout=5.0,
        )
        assert inspected["turn"]["status"] == api_status
        assert inspected["turn"]["usage"] == api_usage
    finally:
        event_loop.cancel()
        await asyncio.gather(event_loop, return_exceptions=True)
        await ipc.close()

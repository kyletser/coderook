from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx

from code_rook.core.transport.socket_client import SocketClient


# 功能：验证项目切换在同一 Core 进程内完成，并保留浏览器认证会话
# 设计：通过真实 IPC 获取稳定 Web URL，再以自动建立的浏览器 Cookie 切换项目并复查 PID、workspace 与会话归属
async def test_project_switch_rebinds_workspace_without_restarting_core(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    api_port: int,
    ipc_token: str,
    tmp_path: Path,
) -> None:
    target = tmp_path / "selected-project"
    target.mkdir()
    second_target = tmp_path / "second-project"
    second_target.mkdir()
    ipc = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await ipc.connect()
    event_loop = asyncio.create_task(ipc.run_event_loop())
    try:
        launch = await ipc.send_command("web.launch", {})
        origin = f"http://127.0.0.1:{api_port}"
        assert launch["url"] == f"{origin}/"
        async with httpx.AsyncClient(base_url=origin, timeout=10.0) as browser:
            bootstrapped = await browser.get("/v1/web/session")
            csrf = bootstrapped.json()["csrf_token"]
            write_headers = {"Origin": origin, "X-CodeRook-CSRF": csrf}
            opened = await browser.post(
                "/v1/projects/open",
                json={"path": str(target)},
                headers=write_headers,
            )
            activated = await browser.post(
                "/v1/projects/activate",
                json={"project_id": opened.json()["id"]},
                headers=write_headers,
            )
            assert activated.status_code == 200
            assert activated.json() == {"workspace": str(target.resolve())}
            assert running_daemon.poll() is None
            assert running_daemon.pid > 0
            session = await browser.get("/v1/web/session")
            assert session.status_code == 200
            assert session.json()["workspace"] == str(target.resolve())
            created = await browser.post(
                "/v1/threads",
                json={"title": "Target workspace", "mode": "chat"},
                headers=write_headers,
            )
            assert created.status_code == 201
            assert created.json()["workspace"] == str(target.resolve())
            first_thread_id = created.json()["id"]
            second_opened = await browser.post(
                "/v1/projects/open",
                json={"path": str(second_target)},
                headers=write_headers,
            )
            second_activated = await browser.post(
                "/v1/projects/activate",
                json={"project_id": second_opened.json()["id"]},
                headers=write_headers,
            )
            assert second_activated.status_code == 200
            assert running_daemon.poll() is None
            second_threads = (await browser.get("/v1/threads")).json()
            assert all(item["id"] != first_thread_id for item in second_threads)
            second_created = await browser.post(
                "/v1/threads",
                json={"title": "Second workspace", "mode": "chat"},
                headers=write_headers,
            )
            second_thread_id = second_created.json()["id"]
            restored = await browser.post(
                "/v1/projects/activate",
                json={"project_id": opened.json()["id"]},
                headers=write_headers,
            )
            assert restored.status_code == 200
            restored_threads = (await browser.get("/v1/threads")).json()
            restored_ids = {item["id"] for item in restored_threads}
            assert first_thread_id in restored_ids
            assert second_thread_id not in restored_ids
    finally:
        event_loop.cancel()
        await asyncio.gather(event_loop, return_exceptions=True)
        await ipc.close()


# 功能：验证真实 daemon 的 HTTP 与 IPC 客户端读取同一个 durable thread 投影
# 设计：HTTP 创建 thread 后分别经 HTTP list 和 IPC session.list 查询相同 id，覆盖双入口一致性；
# 同时断言无 Bearer token 的请求被 401 拒绝，覆盖强制认证
async def test_http_and_ipc_share_durable_threads(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    api_port: int,
    ipc_token: str,
    api_token: str,
) -> None:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{api_port}",
        timeout=5.0,
    ) as anonymous:
        denied = await anonymous.get("/v1/threads")
        assert denied.status_code == 401
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{api_port}",
        headers={"Authorization": f"Bearer {api_token}"},
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

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from code_rook.core.transport.socket_client import SocketClient


# 功能：验证 agent.run 命令返回非空 run_id，且 daemon 随即广播 run.started 事件
# 设计：用 SocketClient 封装 IPC 层，asyncio.Event 等待事件而非轮询，
#       timeout=5s 防测试挂起；run.started 在 LLM 调用前触发，无需真实 API Key
async def test_agent_run_returns_run_id_and_emits_started(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    ipc_token: str,
) -> None:
    client = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await client.connect()

    started_event: asyncio.Event = asyncio.Event()
    received: dict[str, Any] = {}

    async def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            received.update(event)
            started_event.set()

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        result = await client.send_command("agent.run", {"goal": "hello"})

        assert result.get("run_id"), "run_id must be non-empty"
        returned_run_id: str = result["run_id"]

        await asyncio.wait_for(started_event.wait(), timeout=5.0)
        assert received.get("run_id") == returned_run_id
        assert received.get("goal") == "hello"
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


# 功能：验证 Goal IPC 可在不调用真实模型时完成创建、查询、编辑、显式完成和清除
# 设计：用 start=false 隔离模型执行，只通过真实 daemon/socket 检查持久控制面的端到端协议
async def test_goal_control_plane_roundtrip_without_model(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    ipc_token: str,
) -> None:
    client = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())

    # 给每个 IPC 步骤设置独立超时，失败时能定位具体控制命令
    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.wait_for(
            client.send_command(method, params),
            timeout=5.0,
        )

    try:
        session = await send(
            "session.create",
            {"mode": "chat", "title": "goal test"},
        )
        session_id = str(session["session_id"])
        created = await send(
            "goal.create",
            {
                "session_id": session_id,
                "objective": "ship verified goal",
                "completion_criteria": ["tests pass"],
                "start": False,
            },
        )
        goal_id = str(created["goal"]["id"])

        current = await send("goal.get", {"session_id": session_id})
        assert current["goal"]["id"] == goal_id
        edited = await send(
            "goal.edit",
            {"session_id": session_id, "objective": "ship edited goal"},
        )
        assert edited["goal"]["objective"] == "ship edited goal"
        listed = await send("goal.list", {"session_id": session_id})
        assert [item["id"] for item in listed["goals"]] == [goal_id]
        completed = await send(
            "goal.complete",
            {"goal_id": goal_id, "summary": "verified through IPC"},
        )
        assert completed["goal"]["status"] == "completed"
        assert completed["goal"]["completion_evidence"][0]["reference"] == (
            "user://confirmation"
        )
        replacement = await send(
            "goal.create",
            {
                "session_id": session_id,
                "objective": "temporary goal",
                "start": False,
            },
        )
        cleared = await send("goal.clear", {"goal_id": replacement["goal"]["id"]})
        assert cleared["goal"]["status"] == "cleared"
        empty = await send("goal.get", {"session_id": session_id})
        assert empty["goal"] is None
        automatic = await send(
            "goal.create",
            {
                "session_id": session_id,
                "objective": "bounded automatic goal",
                "auto_continue": True,
                "completion_criteria": ["evidence verified"],
                "start": False,
            },
        )
        decision = await send(
            "goal.continue_decision",
            {"goal_id": automatic["goal"]["id"]},
        )
        assert decision["decision"]["should_continue"] is True
        assert decision["decision"]["remaining_auto_turns"] == 3
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


# 功能：验证两个独立客户端同时订阅后，其中一个触发 agent.run，两个都能收到 run.started 广播
# 设计：两个 SocketClient 并行等待事件（asyncio.gather），确认 IpcEventBroadcaster 的扇出语义；
#       不需要两个客户端都发命令，只验证广播覆盖所有订阅者
async def test_two_clients_both_receive_broadcast(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    ipc_token: str,
) -> None:
    client1 = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    client2 = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await client1.connect()
    await client2.connect()

    event1: asyncio.Event = asyncio.Event()
    event2: asyncio.Event = asyncio.Event()

    async def on_event1(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            event1.set()

    async def on_event2(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            event2.set()

    client1.on_event(on_event1)
    client2.on_event(on_event2)

    loop1 = asyncio.create_task(client1.run_event_loop())
    loop2 = asyncio.create_task(client2.run_event_loop())

    try:
        await client1.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        await client2.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        await client1.send_command("agent.run", {"goal": "broadcast test"})

        await asyncio.wait_for(
            asyncio.gather(event1.wait(), event2.wait()),
            timeout=5.0,
        )
    finally:
        loop1.cancel()
        loop2.cancel()
        await asyncio.gather(loop1, loop2, return_exceptions=True)
        await client1.close()
        await client2.close()


# 功能：验证客户端收到 run.started 后立即重连仍可按 run_id 回放持久化事件
# 设计：不增加磁盘等待时间，直接以 replayed_count 断言持久化先于对外广播的运行时契约
async def test_disconnect_and_replay_from_run(
    running_daemon: subprocess.Popen[bytes],
    free_port: int,
    ipc_token: str,
) -> None:
    # Phase 1: trigger a run and wait for run.started to be written to disk
    client1 = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await client1.connect()

    started_event: asyncio.Event = asyncio.Event()
    run_id_holder: list[str] = []

    async def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "run.started":
            run_id_holder.append(event.get("run_id", ""))
            started_event.set()

    client1.on_event(on_event)
    loop1 = asyncio.create_task(client1.run_event_loop())

    try:
        await client1.send_command("event.subscribe", {"topics": ["run.*"], "scope": "global"})
        await client1.send_command("agent.run", {"goal": "replay test"})
        await asyncio.wait_for(started_event.wait(), timeout=5.0)
    finally:
        loop1.cancel()
        await asyncio.gather(loop1, return_exceptions=True)
        await client1.close()

    assert run_id_holder, "run.started was never received"
    run_id = run_id_holder[0]

    # Phase 2: reconnect with replay_from_run and verify replayed_count > 0
    client2 = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await client2.connect()
    loop2 = asyncio.create_task(client2.run_event_loop())

    try:
        result = await client2.send_command(
            "event.subscribe",
            {
                "topics": ["run.*"],
                "scope": "global",
                "replay_from_run": run_id,
            },
        )
        assert result.get("replayed_count", 0) > 0, (
            f"Expected replayed_count > 0 for run_id={run_id!r}, got {result}"
        )
    finally:
        loop2.cancel()
        await asyncio.gather(loop2, return_exceptions=True)
        await client2.close()

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient


async def _start_mock_server(
    handler: Any,
) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port: int = server.sockets[0].getsockname()[1]
    return server, port


# 功能：验证 send_command 向 mock server 发送 JSON-RPC 请求并正确解析响应 result
# 设计：用 asyncio.start_server + port 0 启动内存中的 mock server，避免依赖真实 daemon；
#       loop_task 并发运行 run_event_loop，使 send_command 的 future 能被 _dispatch 解析
async def test_send_command_returns_result() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"pong": True}}
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        result = await asyncio.wait_for(
            client.send_command("core.ping", {"client": "test"}),
            timeout=2.0,
        )
        assert result == {"pong": True}

        await loop_task
        await client.close()


# 功能：验证 server 返回 JSON-RPC error 时 send_command 抛出 IpcError 并携带正确错误码
# 设计：mock server 返回 error 对象（code=-32601），断言异常类型和 code 属性，确认客户端的错误路径处理
async def test_send_command_raises_ipc_error() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32601, "message": "Method not found"},
        }
        writer.write(json.dumps(resp).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        with pytest.raises(IpcError) as exc_info:
            await asyncio.wait_for(
                client.send_command("core.nope", {}),
                timeout=2.0,
            )
        assert exc_info.value.code == -32601

        await loop_task
        await client.close()


# 功能：验证 server 推送 kind=event 的消息时，on_event 注册的 handler 能收到 event 字典
# 设计：server 先返回 RPC 响应（解除 send_command 的等待），再推送事件；用 asyncio.Event 等待 handler 被调用，
#       避免 sleep 轮询；断言 event 内容中的 type 字段是否正确
async def test_event_push_routed_to_handler() -> None:
    received_events: list[dict[str, Any]] = []
    push_done = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        req = json.loads(line)
        resp = {"jsonrpc": "2.0", "id": req["id"], "result": {"subscription_id": "sub-1"}}
        writer.write(json.dumps(resp).encode() + b"\n")
        push = {"kind": "event", "event": {"type": "run.started", "run_id": "r1"}}
        writer.write(json.dumps(push).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)

        async def collect(event_data: dict[str, Any]) -> None:
            received_events.append(event_data)
            push_done.set()

        client.on_event(collect)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())

        await asyncio.wait_for(
            client.send_command("event.subscribe", {"topics": ["run.*"]}),
            timeout=2.0,
        )
        await asyncio.wait_for(push_done.wait(), timeout=2.0)

        assert len(received_events) == 1
        assert received_events[0]["type"] == "run.started"

        await loop_task
        await client.close()


# 功能：验证 server 关闭连接后 run_event_loop 正常退出（不挂起）
# 设计：mock server 立即关闭写流，client readline 收到空字节后退出循环；
#       用 asyncio.wait_for 设置超时，防止 loop 意外挂起导致测试卡死
async def test_run_event_loop_exits_on_server_close() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        await asyncio.wait_for(client.run_event_loop(), timeout=2.0)
        await client.close()


async def test_cancelled_command_removes_pending_future() -> None:
    request_received = asyncio.Event()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        request_received.set()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port)
        await client.connect()
        command_task = asyncio.create_task(client.send_command("slow.command", {}))
        await asyncio.wait_for(request_received.wait(), timeout=2.0)

        command_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command_task

        assert client._pending == {}  # type: ignore[attr-defined]
        await client.close()


async def test_client_authenticates_before_sending_business_command() -> None:
    token = "c" * 43

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        auth = json.loads(await reader.readline())
        assert auth["method"] == "core.authenticate"
        assert auth["params"] == {"token": token}
        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": auth["id"],
                    "result": {"authenticated": True},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()

        command = json.loads(await reader.readline())
        writer.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": command["id"], "result": {"pong": True}}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port, auth_token=token)
        await client.connect()
        loop_task = asyncio.create_task(client.run_event_loop())
        result = await client.send_command("core.ping", {})
        assert result == {"pong": True}
        await loop_task
        await client.close()


async def test_client_closes_connection_when_authentication_fails() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        auth = json.loads(await reader.readline())
        writer.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": auth["id"],
                    "error": {"code": -32002, "message": "Authentication failed"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        await reader.read()
        writer.close()
        await writer.wait_closed()

    server, port = await _start_mock_server(handle)
    async with server:
        client = SocketClient("127.0.0.1", port, auth_token="x" * 43)
        with pytest.raises(IpcError) as exc_info:
            await client.connect()
        assert exc_info.value.code == -32002
        assert client._writer is None  # type: ignore[attr-defined]


# 功能：验证单个 IPC 事件处理器失败不会阻断同一事件的其他处理器
# 设计：注册先抛错后收集的两个回调并直接分发事件，断言后者仍收到完整负载
async def test_event_handler_failure_isolated_from_following_handlers() -> None:
    client = SocketClient("127.0.0.1", 7437)
    received: list[dict[str, Any]] = []

    # 模拟前端某个局部渲染回调失败
    async def fail(_event: dict[str, Any]) -> None:
        raise RuntimeError("broken renderer")

    # 收集后续处理器收到的事件
    async def collect(event: dict[str, Any]) -> None:
        received.append(event)

    client.on_event(fail)
    client.on_event(collect)
    await client._dispatch(  # type: ignore[attr-defined]
        b'{"kind":"event","event":{"type":"run.started","run_id":"r1"}}\n'
    )

    assert received == [{"type": "run.started", "run_id": "r1"}]


def test_client_from_config_reads_token_file(tmp_path: Path) -> None:
    token_path = tmp_path / "ipc-token"
    token_path.write_text("z" * 43 + "\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    config = CodeRookConfig(host="127.0.0.1", port=1234, ipc_token_file=str(token_path))

    client = SocketClient.from_config(config)

    assert client._auth_token == "z" * 43  # type: ignore[attr-defined]


def test_client_from_config_rejects_missing_token(tmp_path: Path) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "missing"))
    with pytest.raises(IpcTokenError, match="does not exist"):
        SocketClient.from_config(config)

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from code_rook.core.bus.envelope import AUTH_FAILED, AUTH_REQUIRED
from code_rook.core.transport.socket_server import SocketServer


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 功能：验证客户端断开后 SocketServer 调用 broadcaster.unsubscribe(writer) 清理订阅
# 设计：用内联 MockBroadcaster 捕获 unsubscribe 调用并设置 asyncio.Event，避免 sleep 轮询；
#       等待 Event 而非断言调用次数，确保时序正确性而不依赖竞态假设
async def test_broadcaster_unsubscribe_called_on_disconnect() -> None:
    unsubscribed = asyncio.Event()

    class MockBroadcaster:
        def unsubscribe(self, writer: object) -> None:
            unsubscribed.set()

    port = _free_port()
    server = SocketServer("127.0.0.1", port, broadcaster=MockBroadcaster())  # type: ignore[arg-type]
    await server.start()

    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()

        await asyncio.wait_for(unsubscribed.wait(), timeout=2.0)
    finally:
        await server.stop()


# 功能：验证不传入 broadcaster 时 SocketServer 仍可正常启动和停止（backward-compatible 默认值）
# 设计：直接实例化 SocketServer(host, port)（无 broadcaster），start/stop 不抛异常即为通过；
#       回归测试确保新参数的默认值 None 不破坏现有调用方
async def test_no_broadcaster_server_starts_and_stops() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port)
    await server.start()
    await server.stop()


async def _send_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    method: str,
    params: dict[str, Any],
    request_id: str,
) -> dict[str, Any]:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    writer.write(json.dumps(request).encode() + b"\n")
    await writer.drain()
    return json.loads(await asyncio.wait_for(reader.readline(), timeout=2.0))


async def test_authenticated_server_rejects_business_command_as_first_frame() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port, auth_token="s" * 43)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        response = await _send_request(reader, writer, "core.ping", {}, "business")
        assert response["error"]["code"] == AUTH_REQUIRED
        assert await asyncio.wait_for(reader.read(), timeout=2.0) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_authenticated_server_rejects_wrong_token_and_closes() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port, auth_token="s" * 43)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        response = await _send_request(
            reader,
            writer,
            "core.authenticate",
            {"token": "w" * 43},
            "auth",
        )
        assert response["error"]["code"] == AUTH_FAILED
        assert response["error"]["message"] == "Authentication failed"
        assert await asyncio.wait_for(reader.read(), timeout=2.0) == b""
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_authenticated_server_allows_commands_without_tracing_token() -> None:
    token = "s" * 43
    records: list[object] = []

    class TraceCapture:
        def emit(self, record: object) -> None:
            records.append(record)

    async def ping(_params: dict[str, Any]) -> dict[str, bool]:
        return {"pong": True}

    port = _free_port()
    server = SocketServer(
        "127.0.0.1",
        port,
        trace=TraceCapture(),  # type: ignore[arg-type]
        auth_token=token,
    )
    server.register("core.ping", ping)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        authenticated = await _send_request(
            reader,
            writer,
            "core.authenticate",
            {"token": token},
            "auth",
        )
        assert authenticated["result"] == {"authenticated": True}
        assert records == []

        pong = await _send_request(reader, writer, "core.ping", {}, "ping")
        assert pong["result"] == {"pong": True}
        assert len(records) == 2
        assert token not in repr(records)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


async def test_socket_server_refuses_non_loopback_bind() -> None:
    server = SocketServer("0.0.0.0", _free_port())
    with pytest.raises(SystemExit, match="non-loopback"):
        await server.start()

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest

from code_rook.core.bus.envelope import AUTH_FAILED, AUTH_REQUIRED
from code_rook.core.transport.socket_server import (
    SocketServer,
    _request_id_from_line,
    get_connection_writer,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# 功能：验证连接限流响应能从待处理帧保留原始 JSON-RPC 请求 ID
# 设计：覆盖合法字符串及非法整数、布尔 ID，确保客户端 Future 可结束且无效帧仍安全回落到 null
def test_request_id_extraction_for_backpressure() -> None:
    assert _request_id_from_line(b'{"jsonrpc":"2.0","id":"req-7","method":"x"}\n') == "req-7"
    assert _request_id_from_line(b'{"id":8}') is None
    assert _request_id_from_line(b'{"id":true}') is None
    assert _request_id_from_line(b'not-json') is None


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


# 功能：验证断线清理完成后才结束的命令不能遗留该 writer 新建的订阅
# 设计：让 handler 等到客户端 EOF 和首次 cleanup 后再注册订阅，依靠任务完成回调触发第二次清理并用事件精确对账竞态
async def test_late_command_subscription_is_removed_after_disconnect() -> None:
    handler_started = asyncio.Event()
    allow_registration = asyncio.Event()
    initial_cleanup = asyncio.Event()
    late_subscription_removed = asyncio.Event()

    class MockBroadcaster:
        # 初始化 writer 活动集与首次清理计数
        def __init__(self) -> None:
            self.active: set[object] = set()
            self.cleanup_calls = 0

        # 在 handler 后半段登记当前连接 writer
        def subscribe(self, writer: object) -> None:
            self.active.add(writer)

        # 区分 EOF 首次清理与任务完成后的迟到订阅清理
        def unsubscribe(self, writer: object) -> bool:
            self.cleanup_calls += 1
            was_active = writer in self.active
            self.active.discard(writer)
            if self.cleanup_calls == 1:
                initial_cleanup.set()
            if was_active:
                late_subscription_removed.set()
            return was_active

    broadcaster = MockBroadcaster()

    # 等客户端断开后才模拟 event.subscribe handler 注册订阅
    async def delayed_subscribe(_params: dict[str, Any]) -> dict[str, bool]:
        handler_started.set()
        await allow_registration.wait()
        broadcaster.subscribe(get_connection_writer())
        return {"subscribed": True}

    port = _free_port()
    server = SocketServer(
        "127.0.0.1",
        port,
        broadcaster=broadcaster,  # type: ignore[arg-type]
    )
    server.register("event.delayed_subscribe", delayed_subscribe)
    await server.start()
    try:
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        request = {
            "jsonrpc": "2.0",
            "id": "late-subscribe",
            "method": "event.delayed_subscribe",
            "params": {},
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        await asyncio.wait_for(handler_started.wait(), timeout=2.0)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(initial_cleanup.wait(), timeout=2.0)

        allow_registration.set()
        await asyncio.wait_for(late_subscription_removed.wait(), timeout=2.0)

        assert broadcaster.active == set()
    finally:
        allow_registration.set()
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


# 功能：超过 StreamReader limit（64MB）的超大帧返回结构化 INVALID_REQUEST 错误而不是裸断连
# 设计：发送略超 limit 的未认证帧，覆盖 readline 抛 ValueError 的真实路径
async def test_oversize_frame_returns_structured_error() -> None:
    port = _free_port()
    server = SocketServer("127.0.0.1", port, broadcaster=None)  # type: ignore[arg-type]
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"x" * (64 * 1024 * 1024 + 16) + b"\n")
        await writer.drain()

        raw = await asyncio.wait_for(reader.readline(), timeout=5.0)
        message: dict[str, Any] = json.loads(raw)
        assert message["error"]["code"] == -32600
        assert "too large" in message["error"]["message"]
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    finally:
        await server.stop()

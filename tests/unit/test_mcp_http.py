from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from code_rook.core.mcp.client import McpClient


# 功能：验证 Streamable HTTP 完成 initialize/session 握手并从 SSE 列出工具
# 设计：MockTransport 依次处理 initialize、notification 和 tools/list，检查 session header 续传
async def test_streamable_http_initializes_session_and_lists_tools() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = str(payload["method"])
        calls.append(method)
        assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
        if method == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session-http"},
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
                },
            )
        assert request.headers["Mcp-Session-Id"] == "session-http"
        if method == "notifications/initialized":
            return httpx.Response(202)
        body = (
            "event: message\n"
            "data: "
            + json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "lookup",
                                "description": "Lookup docs",
                                "inputSchema": {"type": "object"},
                            }
                        ]
                    },
                }
            )
            + "\n\n"
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    client = McpClient()
    await client.connect_streamable_http(
        "http://127.0.0.1:3000/mcp",
        transport=httpx.MockTransport(handler),
    )

    tools = await client.list_tools()
    await client.close()

    assert calls == ["initialize", "notifications/initialized", "tools/list"]
    assert tools[0].name == "lookup"
    assert tools[0].input_schema == {"type": "object"}


# 功能：验证远端明文 HTTP MCP endpoint 在发送凭据或请求前被拒绝
# 设计：直接连接非 loopback HTTP URL，断言安全校验抛错且无需网络 transport
async def test_streamable_http_requires_tls_for_remote_endpoint() -> None:
    client = McpClient()

    with pytest.raises(ValueError, match="require HTTPS"):
        await client.connect_streamable_http("http://mcp.example.com/mcp")


# 功能：验证 MCP resources 与 prompts 通过独立协议方法发现和读取
# 设计：MockTransport 返回最小 initialize 能力和四种响应，断言不会把资源或 prompt 包装成 tool
async def test_streamable_http_supports_resources_and_prompts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"resources": {}, "prompts": {}},
            }
        elif method == "notifications/initialized":
            return httpx.Response(202)
        elif method == "resources/list":
            result = {
                "resources": [
                    {"uri": "file:///guide", "name": "guide", "mimeType": "text/plain"}
                ]
            }
        elif method == "resources/read":
            result = {"contents": [{"uri": "file:///guide", "text": "hello"}]}
        elif method == "prompts/list":
            result = {
                "prompts": [
                    {"name": "review", "arguments": [{"name": "path", "required": True}]}
                ]
            }
        else:
            result = {"description": "review", "messages": []}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload.get("id"), "result": result},
        )

    client = McpClient()
    await client.connect_streamable_http(
        "http://localhost:3000/mcp",
        transport=httpx.MockTransport(handler),
    )

    resources = await client.list_resources()
    contents = await client.read_resource("file:///guide")
    prompts = await client.list_prompts()
    prompt = await client.get_prompt("review", {"path": "src/app.py"})
    await client.close()

    assert resources[0].uri == "file:///guide"
    assert contents[0]["text"] == "hello"
    assert prompts[0].name == "review"
    assert prompt["description"] == "review"


# 功能：验证 GET 服务端消息流断线后携带 Last-Event-ID 重连且消息不重复
# 设计：MockTransport 返回两个有限 SSE 响应，检查第二次 GET 的游标和按序通知队列
async def test_streamable_http_get_stream_reconnects_from_event_cursor() -> None:
    get_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_calls
        if request.method == "GET":
            get_calls += 1
            if get_calls == 2:
                assert request.headers["Last-Event-ID"] == "7"
            event_id = "7" if get_calls == 1 else "8"
            return httpx.Response(
                200,
                text=(
                    f"id: {event_id}\n"
                    "event: message\n"
                    f'data: {{"jsonrpc":"2.0","method":"notice.{event_id}"}}\n\n'
                ),
                headers={"content-type": "text/event-stream"},
            )
        payload = json.loads(request.content)
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "session-get"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
            },
        )

    client = McpClient()
    await client.connect_streamable_http(
        "http://localhost:3000/mcp",
        transport=httpx.MockTransport(handler),
    )
    client.start_server_stream(max_reconnects=1)

    first = await client.next_server_message(timeout=1)
    second = await client.next_server_message(timeout=1)
    await client.close()

    assert first["method"] == "notice.7"
    assert second["method"] == "notice.8"
    assert get_calls == 2


# 功能：验证取消中的 HTTP tool call 会向服务端发送 notifications/cancelled
# 设计：阻塞 tools/call handler 后取消本地 task，断言标准 requestId 被独立 POST 回服务端
async def test_streamable_http_cancel_notifies_server() -> None:
    started = asyncio.Event()
    cancellations: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "notifications/cancelled":
            cancellations.append(payload["params"])
            return httpx.Response(202)
        if method == "tools/call":
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            headers={"Mcp-Session-Id": "session-cancel"},
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"protocolVersion": "2025-03-26", "capabilities": {}},
            },
        )

    client = McpClient()
    await client.connect_streamable_http(
        "http://localhost:3000/mcp",
        transport=httpx.MockTransport(handler),
    )
    task = asyncio.create_task(client.call_tool("slow", {}))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await client.close()

    assert cancellations == [{"requestId": 2, "reason": "CodeRook cancelled tools/call"}]

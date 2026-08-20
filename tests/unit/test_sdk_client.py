from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from code_rook.core.runtime.models import RuntimeEventRecord, ThreadRecord
from code_rook.sdk import AsyncCodeRookClient, CodeRookClient, SdkError


# 构造可通过 SDK Pydantic 校验的 thread 响应
def _thread() -> ThreadRecord:
    now = datetime(2026, 8, 18, tzinfo=UTC)
    return ThreadRecord(
        id="thread-sdk",
        title="SDK",
        workspace="/workspace",
        created_at=now,
        updated_at=now,
    )


# 功能：验证同步 SDK 自动发送 Bearer token 并解析 thread 模型
# 设计：用 MockTransport 检查请求头/路径，再返回真实模型 JSON，避免依赖运行中 daemon
def test_sync_sdk_lists_threads_with_bearer_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret-token"
        assert request.url.path == "/v1/threads"
        return httpx.Response(200, json=[_thread().model_dump(mode="json")])

    with CodeRookClient(
        "http://127.0.0.1:7438",
        "secret-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        threads = client.list_threads()

    assert threads == [_thread()]


# 功能：验证同步 SDK 暴露 thread 读取与更新接口并按 HTTP 契约发送 PATCH
# 设计：MockTransport 记录方法、路径和 JSON body，返回同一 durable 模型以隔离 daemon
def test_sync_sdk_gets_and_updates_thread() -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        return httpx.Response(200, json=_thread().model_dump(mode="json"))

    with CodeRookClient(
        "http://127.0.0.1:7438",
        "token",
        transport=httpx.MockTransport(handler),
    ) as client:
        fetched = client.get_thread("thread-sdk")
        updated = client.update_thread("thread-sdk", title="Renamed", archived=True)

    assert fetched == _thread()
    assert updated == _thread()
    assert calls == [
        ("GET", "/v1/threads/thread-sdk", None),
        ("PATCH", "/v1/threads/thread-sdk", {"title": "Renamed", "archived": True}),
    ]


# 功能：验证异步 SDK 将非成功响应转换成不泄露凭据的 SdkError
# 设计：MockTransport 固定返回 404 JSON，只断言状态和服务端错误正文
async def test_async_sdk_maps_http_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "secret-token" not in str(request.url)
        return httpx.Response(404, json={"error": "missing turn"})

    async with AsyncCodeRookClient(
        "http://127.0.0.1:7438",
        "secret-token",
        transport=httpx.MockTransport(handler),
    ) as client:
        try:
            await client.list_items("missing")
        except SdkError as exc:
            assert exc.status_code == 404
            assert "missing turn" in str(exc)
        else:
            raise AssertionError("SdkError was not raised")


# 功能：验证 SDK 暴露审批响应与结构化 workspace diff 两个 IDE 所需接口
# 设计：MockTransport 分别检查 POST body 和 GET query，固定客户端不绕过公共 HTTP 契约
async def test_async_sdk_controls_permission_and_workspace_diff() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/permissions/"):
            assert json.loads(request.content)["decision"] == "allow_once"
            return httpx.Response(200, json={"accepted": True})
        assert request.url.path == "/v1/workspace/diff"
        assert request.url.params["scope"] == "unstaged"
        return httpx.Response(200, json={"scope": "unstaged", "files": []})

    async with AsyncCodeRookClient(
        "http://127.0.0.1:7438",
        "token",
        transport=httpx.MockTransport(handler),
    ) as client:
        accepted = await client.respond_permission("tool-1", "allow_once")
        diff = await client.workspace_diff(scope="unstaged")

    assert accepted is True
    assert diff["scope"] == "unstaged"


# 功能：验证同步 SDK 暴露 capabilities 与 usage 协商接口
# 设计：让 MockTransport 按路径返回两个开放字典，断言调用方无需绕过 SDK 手写 HTTP 请求
def test_sync_sdk_exposes_capabilities_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json={"api_version": "v1", "stream_json_schema_versions": [1]},
            )
        assert request.url.path == "/v1/usage"
        return httpx.Response(200, json={"tokens": {"input_tokens": 7}})

    with CodeRookClient(
        "http://127.0.0.1:7438",
        "token",
        transport=httpx.MockTransport(handler),
    ) as client:
        capabilities = client.capabilities()
        usage = client.usage()

    assert capabilities["api_version"] == "v1"
    assert capabilities["stream_json_schema_versions"] == [1]
    assert usage["tokens"] == {"input_tokens": 7}


# 功能：验证异步 SSE 从 Last-Event-ID 游标读取且忽略重复序号
# 设计：返回一条重复 seq=2 和一条新 seq=3，关闭重连后断言只产出新事件
async def test_async_sdk_sse_resumes_without_duplicate_events() -> None:
    duplicate = RuntimeEventRecord(
        thread_id="thread-sdk",
        turn_id="turn-sdk",
        seq=2,
        type="step.finished",
        payload={},
        ts=datetime(2026, 8, 18, tzinfo=UTC),
    )
    fresh = duplicate.model_copy(update={"seq": 3, "type": "turn.completed"})

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Last-Event-ID"] == "2"
        body = (
            f"id: 2\ndata: {duplicate.model_dump_json()}\n\n"
            f"id: 3\ndata: {fresh.model_dump_json()}\n\n"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with AsyncCodeRookClient(
        "http://127.0.0.1:7438",
        "token",
        transport=httpx.MockTransport(handler),
    ) as client:
        events = [
            event
            async for event in client.events(
                "thread-sdk",
                after_seq=2,
                reconnect=False,
            )
        ]

    assert [event.seq for event in events] == [3]
    assert json.loads(events[0].model_dump_json())["type"] == "turn.completed"

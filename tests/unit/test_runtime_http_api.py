from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from code_rook.core.api import HttpApiServer
from code_rook.core.api.auth import bearer_authorized, is_loopback_host, validate_api_binding
from code_rook.core.authority import RuntimeMode
from code_rook.core.receipts.builder import build_turn_receipt
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    ThreadRecord,
    ThreadStatus,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)


# 返回 HTTP API 测试使用的稳定时间
def _now() -> datetime:
    return datetime(2026, 8, 4, 9, 0, tzinfo=UTC)


class _FakeRuntimeApi:
    # 初始化覆盖全部 v1 路由的内存记录
    def __init__(self, workspace: Path) -> None:
        self.thread = ThreadRecord(
            id="thread-1",
            title="HTTP",
            workspace=str(workspace),
            status=ThreadStatus.RUNNING,
            created_at=_now(),
            updated_at=_now(),
        )
        self.turn = TurnRecord(
            id="turn-1",
            thread_id=self.thread.id,
            status=TurnStatus.RUNNING,
            usage={"input_tokens": 3},
            created_at=_now(),
            updated_at=_now(),
        )
        self.items = [
            TurnItemRecord(
                id="message-1",
                turn_id=self.turn.id,
                kind=TurnItemKind.MESSAGE,
                payload={"role": "user", "content": "hello"},
                created_at=_now(),
            )
        ]
        self.events = [
            RuntimeEventRecord(
                thread_id=self.thread.id,
                turn_id=self.turn.id,
                seq=index,
                type="test.event",
                payload={"index": index},
                ts=_now(),
            )
            for index in range(1, 4)
        ]
        self.steering = ""
        self.permission_response: tuple[str, str] | None = None

    @property
    # 返回 Web bootstrap 响应中使用的受限测试工作区
    def workspace_root(self) -> str:
        return self.thread.workspace

    # 返回内存 thread 列表
    async def list_threads(self) -> list[ThreadRecord]:
        return [self.thread]

    # 模拟创建 thread
    async def create_thread(self, title: str, mode: str) -> ThreadRecord:
        assert mode in {"chat", "one_shot"}
        self.thread = self.thread.model_copy(update={"title": title})
        return self.thread

    # 验证 fake thread 存在
    async def ensure_thread(self, thread_id: str) -> None:
        if thread_id != self.thread.id:
            raise ValueError("thread not found")

    # 模拟创建 turn 并记录请求 mode
    async def create_turn(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode,
        _attachments: object = None,
    ) -> TurnRecord:
        assert thread_id == self.thread.id
        assert content == "work"
        self.turn = self.turn.model_copy(update={"mode": mode})
        return self.turn

    # 模拟中断 turn
    async def interrupt_turn(self, turn_id: str) -> TurnRecord:
        assert turn_id == self.turn.id
        self.turn = self.turn.model_copy(update={"status": TurnStatus.INTERRUPTED})
        return self.turn

    # 模拟 steering turn
    async def steer_turn(self, turn_id: str, content: str) -> TurnRecord:
        assert turn_id == self.turn.id
        self.steering = content
        return self.turn

    # 返回严格大于 cursor 的连续事件
    async def list_events(
        self,
        thread_id: str,
        after_seq: int,
        limit: int = 1000,
    ) -> list[RuntimeEventRecord]:
        assert thread_id == self.thread.id
        return [event for event in self.events if event.seq > after_seq][:limit]

    # 返回 turn items
    async def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        assert turn_id == self.turn.id
        return self.items

    # 返回由同一 durable records 构建的 receipt
    async def get_receipt(self, turn_id: str) -> Any:
        assert turn_id == self.turn.id
        return build_turn_receipt(self.turn, self.items, self.events)

    # 返回测试能力集
    async def capabilities(self) -> dict[str, Any]:
        return {"api_version": "v1"}

    # 返回测试用量聚合
    async def usage(self) -> dict[str, Any]:
        return {"tokens": {"input_tokens": 3}, "cost": "unknown"}

    # 记录 IDE 经 HTTP 提交的审批响应
    async def respond_permission(
        self,
        tool_use_id: str,
        decision: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.permission_response = (tool_use_id, decision)
        return {"tool_use_id": tool_use_id, "accepted": True}

    # 返回最小结构化 diff 供 HTTP 路由测试
    async def workspace_diff(self, *, scope: str, path: str) -> dict[str, object]:
        return {"scope": scope, "path": path, "files": []}


# 启动随机端口 HTTP API 并返回 server 与 base URL
async def _start_server(
    tmp_path: Path,
    token: str = "test-token",
) -> tuple[HttpApiServer, _FakeRuntimeApi, str]:
    service = _FakeRuntimeApi(tmp_path)
    server = HttpApiServer("127.0.0.1", 0, token, service)  # type: ignore[arg-type]
    host, port = await server.start()
    return server, service, f"http://{host}:{port}"


# 从 SSE 流读取指定数量的事件 id
async def _read_sse_ids(url: str, count: int) -> list[int]:
    ids: list[int] = []
    async with httpx.AsyncClient(
        timeout=2.0,
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        async with client.stream("GET", url) as response:
            assert response.status_code == 200
            assert response.headers["X-CodeRook-API-Version"] == "v1"
            async for line in response.aiter_lines():
                if line.startswith("id: "):
                    ids.append(int(line[4:]))
                    if len(ids) == count:
                        break
    return ids


# 功能：验证回环判断、非回环 token 强制和 bearer 常量时间认证语义
# 设计：纯函数覆盖 IPv4、IPv6、通配绑定及正确/错误 header，不占用真实网络端口
def test_http_auth_requires_token_for_non_loopback() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    with pytest.raises(ValueError, match="requires CODEROOK_API_TOKEN"):
        validate_api_binding("0.0.0.0", "")
    validate_api_binding("0.0.0.0", "secret")
    assert bearer_authorized("Bearer secret", "secret")
    assert not bearer_authorized("Bearer wrong", "secret")
    assert not bearer_authorized(None, "secret")
    assert not bearer_authorized(None, "")
    assert not bearer_authorized("Bearer anything", "")
    assert not bearer_authorized("Bearer anything", "   ")


# 功能：验证 v1 JSON API 的 thread、turn、控制、item、receipt、capability 与 usage 路由
# 设计：使用同一 fake runtime facade 发起真实 TCP HTTP 请求，覆盖路由解析和响应序列化边界
async def test_http_json_routes_share_runtime_service(tmp_path: Path) -> None:
    server, service, base_url = await _start_server(tmp_path)
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=2.0,
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.get("/v1/threads")
            assert response.status_code == 200
            assert response.json()[0]["id"] == "thread-1"

            response = await client.post(
                "/v1/threads",
                json={"title": "Created", "mode": "chat"},
            )
            assert response.status_code == 201
            assert response.json()["title"] == "Created"

            response = await client.post(
                "/v1/threads/thread-1/turns",
                json={"content": "work", "mode": "plan"},
            )
            assert response.status_code == 202
            assert response.json()["mode"] == "plan"

            response = await client.post(
                "/v1/turns/turn-1/steer",
                json={"content": "focus tests"},
            )
            assert response.status_code == 200
            assert service.steering == "focus tests"

            assert (await client.get("/v1/turns/turn-1/items")).json()[0]["id"] == "message-1"
            receipt = (await client.get("/v1/turns/turn-1/receipt")).json()
            assert receipt["turn_id"] == "turn-1"
            response = await client.get("/v1/capabilities")
            assert response.json()["api_version"] == "v1"
            assert response.headers["X-CodeRook-API-Version"] == "v1"
            assert (await client.get("/v1/usage")).json()["cost"] == "unknown"

            response = await client.post(
                "/v1/permissions/tool-1",
                json={"decision": "allow_once"},
            )
            assert response.json()["accepted"] is True
            assert service.permission_response == ("tool-1", "allow_once")

            response = await client.get(
                "/v1/workspace/diff",
                params={"scope": "unstaged", "path": "src"},
            )
            assert response.json()["scope"] == "unstaged"
            assert response.json()["path"] == "src"

            response = await client.post("/v1/turns/turn-1/interrupt")
            assert response.status_code == 200
            assert response.json()["status"] == "interrupted"
    finally:
        await server.stop()


# 功能：验证 Web 记忆编辑路由把路径 ID 与正文交给受限控制分发器
# 设计：通过真实 HTTP PATCH 捕获 dispatcher 参数，固定新增管理闭环而不依赖用户记忆目录
async def test_http_memory_edit_routes_through_control_dispatcher(tmp_path: Path) -> None:
    service = _FakeRuntimeApi(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []

    # 记录 Web 控制命令并返回最小可序列化结果
    async def dispatch(command: str, payload: dict[str, Any]) -> dict[str, object]:
        calls.append((command, payload))
        return {"memory": {"id": payload["memory_id"], "body": payload["body"]}}

    server = HttpApiServer(
        "127.0.0.1",
        0,
        "test-token",
        service,  # type: ignore[arg-type]
        control_dispatcher=dispatch,
    )
    host, port = await server.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            timeout=2.0,
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.patch(
                "/v1/memories/memory-1",
                json={"body": "Run focused tests."},
            )
        assert response.status_code == 200
        assert calls == [
            (
                "memory.edit",
                {"body": "Run focused tests.", "memory_id": "memory-1"},
            )
        ]
    finally:
        await server.stop()


# 功能：验证配置 token 时所有 HTTP 路由拒绝缺失或错误 bearer 并接受正确凭据
# 设计：对同一 capabilities 路由发送三种 header，排除业务路由差异对鉴权结果的影响
async def test_http_bearer_auth_is_applied_before_routing(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path, token="secret")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            assert (await client.get("/v1/capabilities")).status_code == 401
            assert (
                await client.get(
                    "/v1/capabilities",
                    headers={"Authorization": "Bearer wrong"},
                )
            ).status_code == 401
            assert (
                await client.get(
                    "/v1/capabilities",
                    headers={"Authorization": "Bearer secret"},
                )
            ).status_code == 200
    finally:
        await server.stop()


# 功能：验证非回环 HTTP server 缺少 token 时在绑定 socket 前直接启动失败
# 设计：使用通配 host 与随机端口调用真实 start，断言安全校验先于网络监听发生
async def test_http_server_start_fails_closed_without_remote_token(tmp_path: Path) -> None:
    service = _FakeRuntimeApi(tmp_path)
    server = HttpApiServer("0.0.0.0", 0, "", service)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="requires CODEROOK_API_TOKEN"):
        await server.start()


# 功能：验证回环 HTTP server 即使意外收到空 expected token 也不会关闭鉴权
# 设计：直接以空 token 启动真实 loopback server，分别发送无 header 和任意 Bearer 并断言均为 401
async def test_http_loopback_empty_token_fails_closed(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path, token="")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            assert (await client.get("/v1/capabilities")).status_code == 401
            assert (
                await client.get(
                    "/v1/capabilities",
                    headers={"Authorization": "Bearer anything"},
                )
            ).status_code == 401
    finally:
        await server.stop()


# 功能：验证 Web 启动 URL 使用 fragment、单次票据、HttpOnly Cookie 和 CSRF 写保护
# 设计：通过真实回环 HTTP 交换票据后分别执行读、无 CSRF 写和带 CSRF 写，覆盖完整浏览器认证链
async def test_web_bootstrap_uses_single_use_cookie_and_csrf(tmp_path: Path) -> None:
    server, service, base_url = await _start_server(tmp_path)
    try:
        launch_url, expires_in = server.issue_web_launch_url()
        assert expires_in == 60
        assert "#launch=" in launch_url
        ticket = launch_url.split("#launch=", 1)[1]
        origin = base_url
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.post(
                "/v1/web/bootstrap",
                json={"launch_token": ticket},
                headers={"Origin": origin},
            )
            assert response.status_code == 200
            payload = response.json()
            csrf = payload["csrf_token"]
            assert payload["workspace"] == service.workspace_root
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "SameSite=Strict" in response.headers["set-cookie"]

            duplicate = await client.post(
                "/v1/web/bootstrap",
                json={"launch_token": ticket},
                headers={"Origin": origin},
            )
            assert duplicate.status_code == 401
            assert (await client.get("/v1/web/session")).status_code == 200
            denied = await client.post(
                "/v1/threads",
                json={"title": "Denied", "mode": "chat"},
                headers={"Origin": origin},
            )
            assert denied.status_code == 400
            allowed = await client.post(
                "/v1/threads",
                json={"title": "Web", "mode": "chat"},
                headers={"Origin": origin, "X-CodeRook-CSRF": csrf},
            )
            assert allowed.status_code == 201
    finally:
        await server.stop()


# 功能：验证打包 Web 壳只接受当前 loopback Host 并返回严格浏览器安全头
# 设计：对真实静态 index 分别发送合法和恶意 Host，覆盖 DNS rebinding 防护与 CSP
async def test_web_static_shell_rejects_untrusted_host(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path)
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert "CodeRook Web" in response.text
            assert response.headers["x-frame-options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
            rejected = await client.get("/", headers={"Host": "attacker.example"})
            assert rejected.status_code == 400
    finally:
        await server.stop()


# 功能：验证 SSE 使用 durable seq 重连时不会重复或跳过游标后的事件
# 设计：首次读取 1、2 后断开，再携 after_seq=2 重连读取 3，直接覆盖断线恢复契约
async def test_sse_reconnect_resumes_after_durable_cursor(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path)
    try:
        first = await _read_sse_ids(f"{base_url}/v1/threads/thread-1/events?after_seq=0", 2)
        resumed = await _read_sse_ids(f"{base_url}/v1/threads/thread-1/events?after_seq=2", 1)
        assert first == [1, 2]
        assert resumed == [3]
    finally:
        await server.stop()

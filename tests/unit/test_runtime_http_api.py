from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from code_rook.core.api import HttpApiServer
from code_rook.core.api.auth import bearer_authorized, is_loopback_host, validate_api_binding
from code_rook.core.authority import RuntimeMode
from code_rook.core.configuration import ConfigurationValidationError
from code_rook.core.llm.doctor import ProviderDoctorResult
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
from code_rook.core.workspace import WorkspaceBoundaryError


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
            turn_count=1,
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
        self.display_content: str | None = None
        self.turn_content = ""
        self.permission_response: tuple[str, str, str] | None = None
        self.queue: list[dict[str, object]] = []
        self.turn_query: tuple[int | None, str | None] | None = None
        self.model_selection: tuple[str, str, str] | None = None
        self.thinking_selection: tuple[str, str] | None = None
        self.session_import: tuple[str, str, str] | None = None
        self.compaction_focus: tuple[str, str] | None = None
        self.readiness_refreshed = False

    # 返回包含非 ASCII 文件名的导出正文，覆盖浏览器下载响应头编码
    async def export_thread(self, thread_id: str, export_format: str) -> dict[str, str]:
        assert thread_id == self.thread.id
        assert export_format in {"markdown", "json", "html"}
        return {
            "filename": f"会话-{thread_id}.{export_format}",
            "media_type": "text/html; charset=utf-8",
            "content": "<!doctype html><title>CodeRook export</title>",
        }

    @property
    # 返回 Web bootstrap 响应中使用的受限测试工作区
    def workspace_root(self) -> str:
        return self.thread.workspace

    @property
    # 返回 fake API 是否关闭，保持 SSE 生命周期与真实 RuntimeApiService 一致
    def closed(self) -> bool:
        return False

    # 返回内存 thread 列表
    async def list_threads(self) -> list[ThreadRecord]:
        return [self.thread]

    # 模拟创建 thread
    async def create_thread(self, title: str, mode: str) -> ThreadRecord:
        assert mode in {"chat", "one_shot"}
        self.thread = self.thread.model_copy(update={"title": title})
        return self.thread

    # 记录浏览器上传的可移植会话正文并返回新会话摘要。
    async def import_thread(
        self,
        content: str,
        *,
        filename: str = "",
        title: str = "",
    ) -> dict[str, object]:
        self.session_import = (content, filename, title)
        return {
            "thread": self.thread,
            "imported_messages": 2,
            "source_format": "coderook-json",
        }

    # 记录 Web 为当前会话选择的 Python 扩展 Provider 与模型。
    async def set_thread_model(
        self, thread_id: str, route_id: str, model: str,
    ) -> dict[str, object]:
        self.model_selection = (thread_id, route_id, model)
        return {"thread_id": thread_id, "route_id": route_id, "model": model}

    # 记录 Web 为当前会话选择的模型思考强度。
    async def set_thread_thinking(
        self, thread_id: str, thinking_level: str,
    ) -> dict[str, object]:
        self.thinking_selection = (thread_id, thinking_level)
        return {"thread_id": thread_id, "thinking_level": thinking_level}

    # 模拟 Web 在任务提交前刷新 Provider readiness
    async def refresh_provider_readiness(self) -> dict[str, object]:
        self.readiness_refreshed = True
        return {
            "active_route_id": "aliyun",
            "routes": [],
            "presets": [],
            "readiness": {"status": "provider_verified", "local_ready": True},
        }

    # 模拟 Web 手动压缩并记录用户要求保留的重点
    async def compact_thread(
        self,
        thread_id: str,
        *,
        focus: str = "",
    ) -> dict[str, object]:
        self.compaction_focus = (thread_id, focus)
        return {
            "original_tokens": 1200,
            "compacted_tokens": 500,
            "saved_tokens": 700,
            "summary_tokens": 200,
        }

    # 模拟 Core 持久队列接收浏览器后续消息
    async def queue_message(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode,
        attachments: object = None,
        *,
        display_content: str | None = None,
    ) -> dict[str, object]:
        assert thread_id == self.thread.id
        record: dict[str, object] = {
            "id": "queue-1",
            "thread_id": thread_id,
            "content": content,
            "display_content": display_content or content,
            "mode": mode.value,
            "attachments": attachments or [],
            "status": "queued",
            "error": "",
        }
        self.queue.append(record)
        return record

    # 返回 fake 中当前排队消息
    async def list_queued_messages(self, thread_id: str) -> list[dict[str, object]]:
        assert thread_id == self.thread.id
        return list(self.queue)

    # 从 fake 队列删除指定消息
    async def remove_queued_message(
        self,
        thread_id: str,
        message_id: str,
    ) -> dict[str, object]:
        assert thread_id == self.thread.id
        self.queue = [item for item in self.queue if item["id"] != message_id]
        return {"removed": True}

    # 将 fake blocked 消息恢复为 queued
    async def retry_queued_message(
        self,
        thread_id: str,
        message_id: str,
    ) -> dict[str, object]:
        assert thread_id == self.thread.id
        for item in self.queue:
            if item["id"] == message_id:
                item["status"] = "queued"
        return {"retried": True}

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
        *,
        display_content: str | None = None,
    ) -> TurnRecord:
        assert thread_id == self.thread.id
        self.turn_content = content
        self.display_content = display_content
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

    # 返回 fake turn 页并记录 HTTP 查询参数
    async def list_turns(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        before_turn_id: str | None = None,
    ) -> list[TurnRecord]:
        assert thread_id == self.thread.id
        self.turn_query = (limit, before_turn_id)
        return [self.turn]

    # 返回严格大于 cursor 的连续事件
    async def list_events(
        self,
        thread_id: str,
        after_seq: int,
        limit: int = 1000,
    ) -> list[RuntimeEventRecord]:
        assert thread_id == self.thread.id
        return [event for event in self.events if event.seq > after_seq][:limit]

    # 返回 fake 事件高水位供首次有界回放计算游标
    async def latest_event_seq(self, thread_id: str) -> int:
        assert thread_id == self.thread.id
        return self.events[-1].seq

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
        *,
        session_id: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.permission_response = (tool_use_id, decision, session_id)
        return {"tool_use_id": tool_use_id, "accepted": True}

    # 返回最小结构化 diff 供 HTTP 路由测试
    async def workspace_diff(
        self,
        *,
        scope: str,
        path: str,
        thread_id: str = "",
    ) -> dict[str, object]:
        assert thread_id in {"", "thread-1"}
        return {"scope": scope, "path": path, "files": []}


class _ProviderFailureApi(_FakeRuntimeApi):
    # 用脱敏 Doctor 结果模拟 Provider 配置校验失败
    async def save_provider(self, payload: dict[str, Any]) -> dict[str, object]:
        assert payload["model"] == "deepseek-v4-flash"
        raise ConfigurationValidationError(
            ProviderDoctorResult(
                status="error",
                category="schema",
                route_id="deepseek",
                message="declared tool capability probe failed",
                credential_source="keyring",
                http_status=400,
            )
        )


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
            assert response.json()[0]["turn_count"] == 1

            response = await client.post(
                "/v1/threads",
                json={"title": "Created", "mode": "chat"},
            )
            assert response.status_code == 201
            assert response.json()["title"] == "Created"

            response = await client.post(
                "/v1/threads/import",
                json={"content": '{"schema_version":1}', "filename": "task.json"},
            )
            assert response.status_code == 201
            assert response.json()["imported_messages"] == 2
            assert service.session_import == (
                '{"schema_version":1}',
                "task.json",
                "",
            )

            response = await client.post(
                "/v1/threads/thread-1/model",
                json={"route_id": "local-proxy", "model": "model-b"},
            )
            assert response.status_code == 200
            assert response.json()["model"] == "model-b"
            assert service.model_selection == ("thread-1", "local-proxy", "model-b")

            response = await client.post(
                "/v1/threads/thread-1/thinking",
                json={"thinking_level": "high"},
            )
            assert response.status_code == 200
            assert response.json()["thinking_level"] == "high"
            assert service.thinking_selection == ("thread-1", "high")

            response = await client.post(
                "/v1/threads/thread-1/compact",
                json={"focus": "保留失败原因"},
            )
            assert response.status_code == 200
            assert response.json()["saved_tokens"] == 700
            assert service.compaction_focus == ("thread-1", "保留失败原因")

            response = await client.post(
                "/v1/threads/thread-1/queue",
                json={
                    "content": "internal queued prompt",
                    "display_content": "下一轮继续",
                    "mode": "act",
                },
            )
            assert response.status_code == 201
            assert response.json()["display_content"] == "下一轮继续"
            assert (await client.get("/v1/threads/thread-1/queue")).json()[0][
                "id"
            ] == "queue-1"
            service.queue[0]["status"] = "blocked"
            response = await client.post("/v1/threads/thread-1/queue/queue-1/retry")
            assert response.status_code == 200
            assert service.queue[0]["status"] == "queued"
            response = await client.request(
                "DELETE",
                "/v1/threads/thread-1/queue/queue-1",
                json={},
            )
            assert response.status_code == 200
            assert service.queue == []

            response = await client.post(
                "/v1/threads/thread-1/turns",
                json={
                    "content": "work",
                    "display_content": "!pytest",
                    "mode": "plan",
                },
            )
            assert response.status_code == 202
            assert response.json()["mode"] == "plan"
            assert service.turn_content == "work"
            assert service.display_content == "!pytest"

            response = await client.post(
                "/v1/threads/thread-1/turns",
                json={
                    "content": "review calculator.py",
                    "display_content": "审查 calculator.py",
                    "mode": "review",
                },
            )
            assert response.status_code == 202
            assert response.json()["mode"] == "review"
            assert service.turn_content.startswith("review calculator.py")
            assert "Read-only review contract" in service.turn_content
            assert service.display_content == "审查 calculator.py"

            response = await client.get(
                "/v1/threads/thread-1/turns?limit=31&before=turn-cursor"
            )
            assert response.status_code == 200
            assert response.json()[0]["id"] == "turn-1"
            assert service.turn_query == (31, "turn-cursor")

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
                json={"decision": "allow_once", "session_id": "thread-1"},
            )
            assert response.json()["accepted"] is True
            assert service.permission_response == ("tool-1", "allow_once", "thread-1")

            response = await client.post(
                "/v1/permissions/tool-1",
                json={"decision": "allow_once"},
            )
            assert response.status_code == 400
            assert service.permission_response == ("tool-1", "allow_once", "thread-1")

            # 功能：Web 决策词表在路由层翻译为 PermissionManager 词表
            # 设计：allow_session 直传曾被 manager 判为拒绝且不落盘，用户无限重试；
            # 用词表内每个 Web 决策逐个提交，断言翻译映射而不是 manager 收到原词
            decisions = {
                "allow_session": "session_allow",
                "allow_always": "always_allow",
                "deny_session": "session_deny",
                "deny_always": "always_deny",
                "deny_once": "deny_once",
            }
            for web_decision, manager_decision in decisions.items():
                await client.post(
                    "/v1/permissions/tool-1",
                    json={"decision": web_decision, "session_id": "thread-1"},
                )
                assert service.permission_response == (
                    "tool-1",
                    manager_decision,
                    "thread-1",
                )

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


# 功能：验证 Web 会话导出可作为同源附件直接下载，同时保留 UTF-8 文件名
# 设计：通过真实 HTTP 响应检查正文、媒体类型和 Content-Disposition，避免只验证 JSON facade
async def test_http_thread_export_supports_direct_browser_download(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path)
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=2.0,
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.get(
                "/v1/threads/thread-1/export?format=html&download=1"
            )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert response.headers["cache-control"] == "no-store"
        assert "filename*=UTF-8''%E4%BC%9A%E8%AF%9D-thread-1.html" in (
            response.headers["content-disposition"]
        )
        assert response.text == "<!doctype html><title>CodeRook export</title>"
    finally:
        await server.stop()


# 功能：验证 Web 的 Agent 交付设置通过同一 typed Core 控制面读取并更新
# 设计：给真实 HTTP server 注入记录型 dispatcher，分别请求 GET 与 PATCH 并核对命令和完整设置快照
async def test_agent_delivery_settings_http_route(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    # 记录 HTTP 路由下发到 Core 的设置命令
    async def dispatch(command: str, params: dict[str, Any]) -> dict[str, object]:
        calls.append((command, params))
        if command == "agent.settings.get":
            return {
                "settings": {
                    "steering_mode": "one-at-a-time",
                    "follow_up_mode": "one-at-a-time",
                }
            }
        return {"settings": params}

    service = _FakeRuntimeApi(tmp_path)
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
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            current = await client.get("/v1/agent/settings")
            updated = await client.patch(
                "/v1/agent/settings",
                json={"steering_mode": "all", "follow_up_mode": "one-at-a-time"},
            )
        assert current.status_code == 200
        assert current.json()["settings"]["steering_mode"] == "one-at-a-time"
        assert updated.status_code == 200
        assert updated.json()["settings"]["steering_mode"] == "all"
        assert calls == [
            ("agent.settings.get", {}),
            (
                "agent.settings.set",
                {"steering_mode": "all", "follow_up_mode": "one-at-a-time"},
            ),
        ]
    finally:
        await server.stop()


# 功能：验证 Web 可以读取并切换当前会话的工作区信任状态
# 设计：通过真实 HTTP 路由调用记录型 typed dispatcher，核对会话 ID 与受限 trust 枚举不会漂移
async def test_session_authority_http_route(tmp_path: Path) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    # 记录 Web 权限路由下发的 typed Core 命令
    async def dispatch(command: str, params: dict[str, Any]) -> dict[str, object]:
        calls.append((command, params))
        trust = params.get("workspace_trust", "untrusted")
        return {"snapshot": {"workspace_trust": trust}}

    service = _FakeRuntimeApi(tmp_path)
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
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            current = await client.get("/v1/threads/thread-1/authority")
            updated = await client.patch(
                "/v1/threads/thread-1/authority",
                json={"workspace_trust": "trusted"},
            )
            invalid = await client.patch(
                "/v1/threads/thread-1/authority",
                json={"workspace_trust": "always"},
            )
        assert current.status_code == 200
        assert current.json()["snapshot"]["workspace_trust"] == "untrusted"
        assert updated.status_code == 200
        assert updated.json()["snapshot"]["workspace_trust"] == "trusted"
        assert invalid.status_code == 400
        assert calls == [
            ("session.get_authority", {"session_id": "thread-1"}),
            (
                "session.set_authority",
                {"session_id": "thread-1", "workspace_trust": "trusted"},
            ),
        ]
    finally:
        await server.stop()


# 功能：验证 Provider Doctor 校验失败通过 HTTP 422 返回脱敏结构而不是通用 500
# 设计：让 fake service 抛出真实 ConfigurationValidationError，并检查前端恢复所需的分类与上游状态码
async def test_http_provider_validation_failure_is_actionable(tmp_path: Path) -> None:
    service = _ProviderFailureApi(tmp_path)
    server = HttpApiServer("127.0.0.1", 0, "test-token", service)  # type: ignore[arg-type]
    host, port = await server.start()
    try:
        async with httpx.AsyncClient(
            base_url=f"http://{host}:{port}",
            timeout=2.0,
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.post(
                "/v1/providers",
                json={"model": "deepseek-v4-flash"},
            )

        assert response.status_code == 422
        assert response.json() == {
            "error": "provider validation failed",
            "code": "provider_validation_failed",
            "category": "schema",
            "message": "declared tool capability probe failed",
            "provider_status": 400,
        }
    finally:
        await server.stop()


# 功能：验证 Web 可通过独立端点在创建 Turn 前刷新 Provider readiness
# 设计：经真实 HTTP POST 调用 fake service，固定端点语义且确保没有误走 route 激活分支
async def test_http_refreshes_provider_readiness(tmp_path: Path) -> None:
    server, service, base_url = await _start_server(tmp_path)
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            response = await client.post("/v1/providers/readiness")

        assert response.status_code == 200
        assert response.json()["readiness"]["local_ready"] is True
        assert service.readiness_refreshed is True
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


# 功能：验证本机 Web 可直接建立 HttpOnly Cookie，同时继续使用 CSRF 保护写请求
# 设计：直接打开稳定 URL 获取会话，再分别执行无 CSRF 与带 CSRF 写请求，覆盖零配置浏览器入口
async def test_web_session_opens_directly_and_keeps_csrf_protection(tmp_path: Path) -> None:
    server, service, base_url = await _start_server(tmp_path)
    try:
        launch_url = server.issue_web_launch_url()
        assert launch_url == f"{base_url}/"
        origin = base_url
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.get("/v1/web/session")
            assert response.status_code == 200
            payload = response.json()
            csrf = payload["csrf_token"]
            assert payload["workspace"] == service.workspace_root
            assert "HttpOnly" in response.headers["set-cookie"]
            assert "SameSite=Strict" in response.headers["set-cookie"]

            compatible = await client.post(
                "/v1/web/bootstrap",
                json={"launch_token": "expired-old-link"},
                headers={"Origin": origin},
            )
            assert compatible.status_code == 200
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


# 功能：验证工作区文件路径越界返回可理解的 400 而不是通用 500
# 设计：让文件服务抛出真实边界异常，经完整 HTTP 请求断言状态码和原始诊断正文
async def test_workspace_boundary_error_is_a_bad_request(tmp_path: Path) -> None:
    server, service, base_url = await _start_server(tmp_path)

    # 模拟工作区解析器拒绝浏览器提交的上级路径
    async def reject_path(**_kwargs: object) -> dict[str, object]:
        raise WorkspaceBoundaryError("path is outside workspace: ..")

    service.list_workspace_files = reject_path  # type: ignore[attr-defined]
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
            response = await client.get(
                "/v1/workspace/files?path=..",
                headers={"Authorization": "Bearer test-token"},
            )
        assert response.status_code == 400
        assert response.json() == {"error": "path is outside workspace: .."}
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


# 功能：验证首次 SSE 可只读取近期窗口且已有游标重连绝不因 tail 跳过事件
# 设计：同一三事件流分别从零游标 tail=1 和非零游标 tail=1 读取，区分首屏优化与可靠续接
async def test_sse_tail_only_limits_initial_history(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path)
    try:
        recent = await _read_sse_ids(
            f"{base_url}/v1/threads/thread-1/events?after_seq=0&tail=1",
            1,
        )
        resumed = await _read_sse_ids(
            f"{base_url}/v1/threads/thread-1/events?after_seq=1&tail=1",
            1,
        )
        assert recent == [3]
        assert resumed == [2]
    finally:
        await server.stop()


# 功能：验证存在浏览器 SSE 长连接时 HTTP server 仍能立即停止
# 设计：保持真实 TCP 流不主动断开，再用短超时约束 stop 必须先关闭客户端后等待监听器
async def test_http_server_stop_closes_active_sse_connection(tmp_path: Path) -> None:
    server, _service, base_url = await _start_server(tmp_path)
    url = httpx.URL(base_url)
    reader, writer = await asyncio.open_connection(url.host, url.port)
    writer.write(
        (
            "GET /v1/threads/thread-1/events?after_seq=3 HTTP/1.1\r\n"
            f"Host: {url.host}:{url.port}\r\n"
            "Authorization: Bearer test-token\r\n"
            "Connection: keep-alive\r\n\r\n"
        ).encode("ascii")
    )
    await writer.drain()
    response_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
    assert b"200 OK" in response_head

    await asyncio.wait_for(server.stop(), timeout=2.0)
    assert await asyncio.wait_for(reader.read(), timeout=1.0) == b""
    writer.close()
    await writer.wait_closed()

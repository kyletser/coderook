from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from code_rook.core.config import CodeRookConfig, _apply_toml
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.errors import ProviderRequestError
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.openai_responses import OpenAIResponsesProvider
from code_rook.core.llm.provider import AnthropicProvider
from code_rook.core.llm.retry import RetryPolicy, parse_retry_after
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.registry import ToolRegistry


@pytest.mark.parametrize("wire", ["chat", "responses"])
@pytest.mark.parametrize("failure", ["read", "disconnect", "401", "503", "exhausted"])
# 功能：验证真实 Provider 到 Loop 的传输恢复、认证拒绝及重试上限
# 设计：两种协议共用 HTTP 假后端，核对重试请求完全相同且错误回溯不泄露服务端秘密
async def test_provider_request_recovery(wire: str, failure: str) -> None:
    requests: list[bytes] = []
    events: list[BaseModel] = []

    # 在首请求或持续失败场景抛错，第二次返回指定协议的完成消息
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1 or failure == "exhausted":
            if failure in {"401", "503"}:
                return httpx.Response(int(failure), json={"error": "PRIVATE_SERVER_BODY"})
            error_type = httpx.RemoteProtocolError if failure == "disconnect" else httpx.ReadTimeout
            raise error_type("PRIVATE_ENDPOINT_AND_KEY", request=request)
        if wire == "chat":
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "done"}, "finish_reason": "stop",
            }]})
        return httpx.Response(200, json={
            "status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "done"},
            ]}],
        })

    # 收集已有重试事件，不新增测试专用运行时路径
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus = EventBus()
    bus.subscribe(collect)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = (
            OpenAICompatibleProvider(
                "test-model", base_url="https://test.invalid/v1", api_key_env="UNUSED",
                api_key="PRIVATE_KEY", client=client,
            ) if wire == "chat" else OpenAIResponsesProvider(
                "test-model", base_url="https://test.invalid/v1",
                api_key="PRIVATE_KEY", client=client,
            )
        )
        loop = AgentLoop(provider, ToolRegistry(), bus, retry_backoff_s=0)
        context = ExecutionContext(run_id="recovery", goal="say done", max_steps=4)
        await loop.run(context)
        if failure == "401":
            assert len(requests) == 1
            assert context.status == "failed"
        elif failure == "exhausted":
            assert len(requests) == 6
            assert context.reason == "transport_error"
        else:
            assert len(requests) == 2
            assert context.status == "success"
            assert context.result == "done"
        assert context.step == 1
        assert all(request == requests[0] for request in requests)
        retries = [e for e in events if e.type == "llm.retry"]  # type: ignore[attr-defined]
        assert len(retries) == (0 if failure == "401" else 5 if failure == "exhausted" else 1)
        assert "PRIVATE" not in json.dumps([event.model_dump() for event in retries])
        assert not any(e.type == "tool.call_started" for e in events)  # type: ignore[attr-defined]
        requests.clear()
        with pytest.raises(ProviderRequestError) as captured:
            await provider.chat([], [], bus, "direct")
        assert "PRIVATE" not in "".join(traceback.format_exception(captured.value))
        assert captured.value.error_kind
        assert captured.value.__context__ is None


@pytest.mark.parametrize("status,expected", [(400, False), (403, False), (429, True), (502, True)])
# 功能：验证 HTTP 分类由结构化状态决定而不是错误正文里的数字
# 设计：正文故意包含 429，确认认证和请求错误不会被消息关键词误判为可重试
def test_http_retry_classification(status: int, expected: bool) -> None:
    request = httpx.Request("POST", "https://test.invalid")
    response = httpx.Response(status, request=request)
    raw = httpx.HTTPStatusError("429", request=request, response=response)
    assert AgentLoop._is_transient_error(ProviderRequestError("test", raw)) is expected


# 功能：验证工具已成功后模型断线只重试下一请求，不重复执行工具副作用
# 设计：真实 Chat Provider 依次返回工具、断线、完成，用计数工具和请求字节验证重试边界
async def test_retry_does_not_repeat_completed_tool() -> None:
    class CounterTool(BaseTool):
        name = "counter"
        description = "Count an operation"
        input_schema: dict[str, object] = {"type": "object", "properties": {}}

        # 初始化副作用计数以检测重复调用
        def __init__(self) -> None:
            self.calls = 0

        # 每次执行递增计数以代表不可重复的修改动作
        async def invoke(self, params: dict[str, object]) -> ToolResult:
            self.calls += 1
            return ToolResult(content="applied")

    requests: list[bytes] = []

    # 在工具执行后的模型请求注入一次连接断开
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "tool_calls", "message": {"tool_calls": [{
                    "id": "operation-1", "type": "function",
                    "function": {"name": "counter", "arguments": "{}"},
                }]},
            }]})
        if len(requests) == 2:
            raise httpx.ReadError("connection closed", request=request)
        return httpx.Response(200, json={"choices": [{
            "finish_reason": "stop", "message": {"content": "done"},
        }]})

    tool = CounterTool()
    registry = ToolRegistry()
    registry.register(tool)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            "test", base_url="https://test.invalid", api_key_env="UNUSED",
            api_key="test-only", client=client,
        )
        context = ExecutionContext(run_id="r1", goal="apply", max_steps=4)
        await AgentLoop(provider, registry, EventBus(), retry_backoff_s=0).run(context)
    assert context.status == "success"
    assert tool.calls == 1
    assert len(requests) == 3
    assert requests[1] == requests[2]


@pytest.mark.parametrize("wire", ["chat", "responses"])
# 功能：验证部分正文和工具 JSON 断流只进入审计，重试请求及重启历史保持干净
# 设计：真实 HTTP 解析器读取半条 SSE 后断开；从磁盘重新打开 Ledger 验证而非只看内存
async def test_partial_stream_is_audit_only(tmp_path: Path, wire: str) -> None:
    requests: list[bytes] = []
    store = SessionStore(tmp_path)
    store.append_message("sess-retry", "user", "say done")
    sink = SessionTranscriptSink(store, "sess-retry", "run")

    class DroppedStream(httpx.AsyncByteStream):
        # 输出部分正文及尚未完整的工具参数后模拟连接重置
        async def __aiter__(self):
            if wire == "chat":
                yield b'data: {"choices":[{"delta":{"content":"FAILED_PARTIAL","tool_calls":[{"index":0,"id":"partial","function":{"name":"edit","arguments":"{"}}]}}]}\n\n'
            else:
                yield b'data: {"type":"response.output_text.delta","delta":"FAILED_PARTIAL"}\n\n'
                yield b'data: {"type":"response.function_call_arguments.delta","item_id":"partial","delta":"{"}\n\n'
            raise httpx.ReadError("PRIVATE transport detail")

    # 首次断流，第二次返回当前协议的正常完成响应
    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.content)
        if len(requests) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=DroppedStream())
        if wire == "chat":
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "done"}, "finish_reason": "stop",
            }]})
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "done"}],
        }]})

    # 在广播回调时直接读磁盘，证明重试通知不早于账本提交
    async def verify_post_commit(event: BaseModel) -> None:
        if getattr(event, "type", "") == "llm.retry":
            saved = store.read_session_events("sess-retry")
            assert saved[-1].type == "llm.retry"
            assert saved[-1].seq == event.ledger_seq  # type: ignore[attr-defined]

    bus = EventBus()
    bus.subscribe(verify_post_commit, critical=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = (
            OpenAICompatibleProvider("test", base_url="https://test.invalid", api_key="test",
                                     api_key_env="UNUSED", client=client)
            if wire == "chat" else
            OpenAIResponsesProvider("test", base_url="https://test.invalid",
                                    api_key="test", client=client)
        )
        context = ExecutionContext(run_id="run", goal="say done", max_steps=2)
        await AgentLoop(provider, ToolRegistry(), bus, transcript=sink,
                        retry_backoff_s=0).run(context)
    reopened = SessionStore(tmp_path)
    messages = reopened.derive_messages("sess-retry")
    assert messages == context.messages
    assert "FAILED_PARTIAL" not in json.dumps(messages)
    assert context.status == "success" and context.step == 1
    assert requests[0] == requests[1] and len(requests) == 2
    events = reopened.read_session_events("sess-retry")
    failed = [e for e in events if e.type == "llm.attempt_finished"
              and e.payload["status"] == "failed"]
    assert len(failed) == 1
    assert "FAILED_PARTIAL" in json.dumps(failed[0].payload)
    assert "partial" in json.dumps(failed[0].payload)
    assert "PRIVATE" not in json.dumps(failed[0].payload)
    assert [e.type for e in events if e.type.startswith("llm.retry")] == [
        "llm.retry", "llm.retry_started",
    ]


# 功能：验证等待退避期间取消会记账且绝不发起第二个请求
# 设计：等待真实重试事件后取消 asyncio task，不用长时间 sleep 或真实网络
async def test_cancel_during_backoff(tmp_path: Path) -> None:
    requests = 0
    waiting = asyncio.Event()
    store = SessionStore(tmp_path)
    store.append_message("sess-retry", "user", "task")

    # 持续返回限流，若取消失效则请求计数会增加
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(429)

    # 告知测试重试已经提交，随后可以取消等待
    async def scheduled(event: BaseModel) -> None:
        if getattr(event, "type", "") == "llm.retry":
            waiting.set()

    bus = EventBus()
    bus.subscribe(scheduled)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider("test", base_url="https://test.invalid",
                                            api_key_env="UNUSED", api_key="test", client=client)
        context = ExecutionContext(run_id="run", goal="task", max_steps=2)
        task = asyncio.create_task(AgentLoop(
            provider, ToolRegistry(), bus, transcript=SessionTranscriptSink(store, "sess-retry", "run"),
            retry_policy=RetryPolicy(initial_delay_s=10, jitter_ratio=0),
        ).run(context))
        await asyncio.wait_for(waiting.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert requests == 1
    kinds = [e.type for e in store.read_session_events("sess-retry")]
    assert "llm.retry_cancelled" in kinds
    assert "llm.retry_started" not in kinds


# 功能：验证正常重试默认五次、指数退避、抖动范围及 Retry-After 上限
# 设计：固定随机端点检查确定性边界，HTTP 日期解析使用过去时间消除时钟波动
def test_retry_policy_defaults_and_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = RetryPolicy()
    assert policy.max_retries == 5
    monkeypatch.setattr("code_rook.core.llm.retry.random.uniform", lambda a, b: a)
    assert [policy.delay(i) for i in range(1, 4)] == [0.45, 0.9, 1.8]
    monkeypatch.setattr("code_rook.core.llm.retry.random.uniform", lambda a, b: b)
    assert policy.delay(1) == 0.55
    assert policy.delay(10) == 10
    assert policy.delay(1, 3) == 3
    assert policy.delay(1, 11) is None
    assert parse_retry_after("2") == 2
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0
    assert parse_retry_after("nan") is None
    assert parse_retry_after("invalid") is None


# 功能：验证用户 TOML 可关闭或调整统一重试策略，错误配置立即报错
# 设计：直接走生产配置合并路径并分层覆盖，确认未指定字段保持上层值
def test_retry_configuration_is_shared() -> None:
    config = CodeRookConfig()
    _apply_toml(config, {"llm": {"retry": {"max_retries": 2, "initial_delay_s": 1}}})
    _apply_toml(config, {"llm": {"retry": {"max_retries": 0}}})
    assert config.llm.retry.max_retries == 0
    assert config.llm.retry.initial_delay_s == 1
    with pytest.raises(SystemExit):
        _apply_toml(config, {"llm": {"retry": {"max_retries": -1}}})
    with pytest.raises(SystemExit):
        _apply_toml(config, {"llm": {"retry": {"typo": 1}}})


@pytest.mark.parametrize("wire", ["chat", "responses"])
@pytest.mark.parametrize("code,retryable", [
    ("rate_limit_exceeded", True), ("invalid_api_key", False),
])
# 功能：验证 HTTP 200 中的结构化流内错误同样进入统一分类，而认证错误不重试
# 设计：首个 SSE error 带秘密正文，后续成功，检查真实请求数量和公开事件脱敏
async def test_stream_error_classification(wire: str, code: str, retryable: bool) -> None:
    calls = 0
    events: list[BaseModel] = []

    # 返回结构化错误而非 HTTP 错误，覆盖兼容 Provider 的流内失败路径
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            payload = {"error": {"code": code, "message": "PRIVATE_BODY"}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="data: " + json.dumps(payload) + "\n\n")
        if wire == "chat":
            return httpx.Response(200, json={"choices": [{
                "message": {"content": "done"}, "finish_reason": "stop",
            }]})
        return httpx.Response(200, json={"status": "completed", "output": [{
            "type": "message", "content": [{"type": "output_text", "text": "done"}],
        }]})

    # 记录公开事件以检查错误正文未泄露
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus = EventBus()
    bus.subscribe(collect)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = (
            OpenAICompatibleProvider("test", base_url="https://test.invalid", api_key="test",
                                     api_key_env="UNUSED", client=client)
            if wire == "chat" else
            OpenAIResponsesProvider("test", base_url="https://test.invalid",
                                    api_key="test", client=client)
        )
        context = ExecutionContext(run_id="stream", goal="task", max_steps=2)
        await AgentLoop(provider, ToolRegistry(), bus, retry_backoff_s=0).run(context)
    assert calls == (2 if retryable else 1)
    assert context.status == ("success" if retryable else "failed")
    assert "PRIVATE" not in json.dumps([e.model_dump(mode="json") for e in events])


# 功能：验证即使注入启用 SDK 重试的客户端，Anthropic 适配器也只请求一次
# 设计：使用真实 SDK 配合 HTTP MockTransport 的 503 响应，避免 MagicMock 漏掉隐式重试
async def test_anthropic_sdk_retries_are_disabled() -> None:
    calls = 0

    # 持续服务端错误用于发现 SDK 自带重试
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": {
            "type": "overloaded_error", "message": "PRIVATE_BODY",
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        sdk = anthropic.AsyncAnthropic(api_key="test", max_retries=5, http_client=http)
        provider = AnthropicProvider("test", client=sdk)
        with pytest.raises(ProviderRequestError) as captured:
            await provider.chat([], [], EventBus(), "sdk")
    assert calls == 1
    assert captured.value.failure_code == "server_error"
    assert captured.value.__context__ is None
    assert "PRIVATE" not in str(captured.value)


# 功能：验证尝试审计写入失败会阻止请求，不边丢日志边继续重试
# 设计：真实 SessionTranscriptSink 注入磁盘写入错误，HTTP 计数证明网络未被调用
async def test_audit_failure_prevents_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-audit", "user", "task")
    sink = SessionTranscriptSink(store, "sess-audit", "run")
    calls = 0

    # 模拟明确的磁盘错误
    def fail(*args: object, **kwargs: object) -> int:
        raise OSError("disk full")

    # 记录不应到达的网络边界
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    monkeypatch.setattr(sink, "append_audit", fail)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider("test", base_url="https://test.invalid", api_key="test",
                                            api_key_env="UNUSED", client=client)
        context = ExecutionContext(run_id="run", goal="task", max_steps=2)
        await AgentLoop(provider, ToolRegistry(), EventBus(), transcript=sink).run(context)
    assert calls == 0
    assert context.reason == "invariant_violation"

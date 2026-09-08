import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from code_rook.core.agent_runtime.summarization import SummaryBus, complete_summary
from code_rook.core.bus.events import LlmTokenEvent, LlmUsageEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.errors import ProviderRequestError
from code_rook.core.llm.retry import RetryPolicy
from code_rook.core.llm.types import LlmResponse
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.model import Session
from code_rook.tui.render import render_event


# 功能：摘要计入持久成本但不覆盖主任务上下文占比，TUI 使用相同语义。
# 设计：真实 SQLite 接收主回答及两次摘要用量，重开数据库检查累计和摘要分项。
async def test_summary_usage_is_accounted_without_replacing_context(tmp_path: Path) -> None:
    bus = EventBus()
    db = tmp_path / "runtime.db"
    runtime = RuntimeService(RuntimeStore(db), workspace=tmp_path)
    bus.subscribe(runtime.record_bus_event)
    ts = "2026-09-08T08:00:00Z"
    session = Session(id="usage-session", mode="chat", status="active", title="usage",
                      created_at=ts, updated_at=ts)
    await runtime.start_turn(session, "usage-run", "work")
    usage = LlmUsageEvent(
        run_id="usage-run", input_tokens=100, output_tokens=10,
        cache_read_input_tokens=5, cache_creation_input_tokens=0,
        context_pct=0.7, model="claude-sonnet-4-6", ts=ts,
    )
    await bus.publish(usage)
    original_cost = (await runtime.get_turn("usage-run")).usage["estimated_cost_usd"]
    summary_bus = SummaryBus(bus)
    await summary_bus.publish(usage.model_copy(update={"context_pct": 0.1}))
    await summary_bus.publish(usage.model_copy(update={"context_pct": 0.2}))
    persisted = RuntimeStore(db).get_turn("usage-run").usage
    assert persisted["input_tokens"] == 300
    assert persisted["summary_input_tokens"] == 200
    assert persisted["summary_output_tokens"] == 20
    assert persisted["summary_cache_read_input_tokens"] == 10
    assert persisted["context_pct"] == 0.7
    assert persisted["estimated_cost_usd"] == pytest.approx(original_cost * 3)
    assert usage.purpose == "response"
    app = MagicMock()
    app._subagent_run_ids = {}
    app._last_context_pct = 0.7
    render_event(app, usage.model_copy(update={"purpose": "summary", "context_pct": 0.1}).model_dump())
    assert app._last_context_pct == 0.7
    app._accumulate_cost.assert_called_once()


# 功能：摘要断流只重发同一请求且不向回答时间线发送部分正文。
# 设计：假 Provider 首次修改请求再失败，次次都检查原文和空工具集以验证请求隔离。
async def test_summary_retries_immutable_request_without_answer_fragments() -> None:
    bus = EventBus()
    seen = []
    audit_events = []

    # 收集摘要事实记录，检查失败片段保留但不会混入正式回答。
    def audit(event_type: str, payload: dict) -> None:
        audit_events.append((event_type, payload))

    # 记录共享总线中可见的重试通知。
    async def collect(event: object) -> None:
        seen.append(event)

    attempts = 0

    # 模拟有输出后断流，第二次返回完整摘要。
    async def chat(**kwargs: object) -> LlmResponse:
        nonlocal attempts
        attempts += 1
        assert kwargs["messages"] == [{"role": "user", "content": "summarize"}]
        assert kwargs["tool_schemas"] == []
        await kwargs["bus"].publish(LlmTokenEvent(run_id="summary", token="partial", ts="now"))
        kwargs["messages"][0]["content"] = "mutated"
        if attempts == 1:
            raise ProviderRequestError("fake", httpx.ReadError("connection closed"))
        return LlmResponse(stop_reason="end_turn", text="complete")

    bus.subscribe(collect)
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    response = await complete_summary(
        provider, messages=[{"role": "user", "content": "summarize"}], bus=bus,
        run_id="summary", system="summary", retry_policy=RetryPolicy(initial_delay_s=0),
        audit=audit,
    )
    assert response.text == "complete"
    assert attempts == 2
    assert [event.type for event in seen] == ["llm.retry"]
    assert [kind for kind, _ in audit_events] == [
        "llm.summary_request", "llm.summary_attempt", "llm.summary_attempt",
    ]
    request, failed, received = [payload for _, payload in audit_events]
    assert request["request"]["messages"] == [{"role": "user", "content": "summarize"}]
    assert len({payload["request_digest"] for _, payload in audit_events}) == 1
    assert len({payload["request_id"] for _, payload in audit_events}) == 1
    assert failed["status"] == "failed" and failed["attempt"] == 1
    assert failed["fragments"]
    assert received["status"] == "received" and received["attempt"] == 2
    assert received["response"]["text"] == "complete"


# 功能：鉴权失败不会被当作网络错误反复重试。
# 设计：构造真实 HTTP 401 异常包装，断言 Provider 仅调用一次。
async def test_summary_authentication_failure_is_not_retried() -> None:
    response = httpx.Response(401, request=httpx.Request("POST", "https://example.test"))
    failure = ProviderRequestError("fake", httpx.HTTPStatusError("auth", request=response.request, response=response))
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=failure)
    with pytest.raises(ProviderRequestError):
        await complete_summary(provider, messages=[], bus=EventBus(), run_id="s", system="s")
    assert provider.chat.await_count == 1


# 功能：重试等待期间可以取消，不继续消耗模型调用。
# 设计：收到真实 retry 事件立即取消任务，不依赖固定睡眠来猜测进度。
async def test_summary_retry_wait_is_cancellable() -> None:
    notified = asyncio.Event()
    bus = EventBus()

    # 仅在进入重试等待前发出测试信号。
    async def collect(event: object) -> None:
        notified.set()

    bus.subscribe(collect)
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=ConnectionError("closed"))
    task = asyncio.create_task(complete_summary(
        provider, messages=[], bus=bus, run_id="s", system="s",
        retry_policy=RetryPolicy(initial_delay_s=10, max_delay_s=10),
    ))
    await asyncio.wait_for(notified.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.chat.await_count == 1

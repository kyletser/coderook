from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import FollowUpMessage, InteractionManager
from code_rook.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.tools.registry import ToolRegistry


# 功能：长纠偏在下一次请求前参与压缩，不能在容量检查后直接塞给模型。
# 设计：首轮模型返回时排入长消息，捕获第二次请求并检查摘要生成器确实看到了纠偏全文。
@pytest.mark.parametrize("queued", [False, True])
async def test_long_steering_is_compacted_before_request(queued: bool) -> None:
    bus = EventBus()
    interaction = InteractionManager(bus)
    interaction.register_run("long-steer")
    provider = MagicMock()
    provider.context_window = 8000
    long_text = "Keep this constraint. " * 3000
    requests = []
    delivered = False

    # 后续消息确认不调用外部服务，此处只验证排队与压缩顺序。
    async def admitted():
        pass

    # 仅领取一次队列消息，防止测试本身产生无限后续任务。
    async def follow_up():
        nonlocal delivered
        if delivered:
            return []
        delivered = True
        return [FollowUpMessage(long_text.strip(), admitted)]

    if queued:
        interaction.bind_follow_up("long-steer", follow_up)

    # 在首轮结束前模拟用户输入，下一轮必须使用包含新要求的压缩结果。
    async def chat(messages, *_args, **_kwargs):
        requests.append(deepcopy(messages))
        if len(requests) == 1 and not queued:
            interaction.steer("long-steer", long_text)
        return LlmResponse(stop_reason="end_turn", text="Answer")

    provider.chat = AsyncMock(side_effect=chat)
    compactor = MagicMock()
    compactor.reserve_tokens = 1000

    # 确认压缩输入包含新增消息，再返回保留该约束的短摘要。
    async def compact(context, *_args, **_kwargs):
        assert context.messages[-1]["content"] == long_text.strip()
        context.messages = [{"role": "user", "content": "Summary: keep this constraint."}]
        return object()

    compactor.compact = AsyncMock(side_effect=compact)
    context = ExecutionContext(run_id="long-steer", goal="Start", max_steps=3)
    await AgentLoop(
        provider, ToolRegistry(), bus, interaction_manager=interaction,
        compactor=compactor, compact_threshold=0.8,
    ).run(context)
    assert context.status == "success", context.reason
    assert len(requests) == 2
    assert compactor.compact.await_count == 1
    assert requests[1][0]["content"] == "Summary: keep this constraint."


# 功能：全缓存命中或缺失 usage 时，新增工具结果仍可在下一次请求前触发压缩。
# 设计：短首轮请求后返回大工具结果，捕获第二次模型请求确保拿到摘要而非超限历史。
@pytest.mark.parametrize("cached", [True, False])
async def test_next_request_compacts_cached_or_missing_usage(cached: bool) -> None:
    provider = MagicMock()
    provider.context_window = 100000 if cached else 8000
    usage = UsageStats(
        input_tokens=0, output_tokens=100,
        cache_read_input_tokens=79000, context_pct=0.79,
    ) if cached else None
    requests = []

    # 首轮使用工具，后续请求检查实际模型上下文。
    async def chat(messages, *_args, **_kwargs):
        requests.append(deepcopy(messages))
        if len(requests) == 1:
            return LlmResponse(stop_reason="tool_use", usage=usage, tool_calls=[
                ToolCallBlock(id="large", name="read", input={"path": "large.txt"}),
            ])
        return LlmResponse(stop_reason="end_turn", text="Done")

    provider.chat = AsyncMock(side_effect=chat)
    registry = ToolRegistry()
    loop = AgentLoop(
        provider, registry, EventBus(), compact_threshold=0.8, tool_result_limit=200000,
    )
    from code_rook.core.tools.base import ToolResult
    loop._invoke_one = AsyncMock(return_value=ToolResult("x" * 100000))
    compactor = MagicMock()
    compactor.reserve_tokens = 16384 if cached else 1000

    # 使用无模型费用的摘要替代，仍由生产 prepare 决定何时压缩。
    async def compact(context, *_args, **_kwargs):
        context.messages = [{"role": "user", "content": "Task summary"}]
        return object()

    compactor.compact = AsyncMock(side_effect=compact)
    loop._compactor = compactor
    context = ExecutionContext(run_id="cache-pressure", goal="Read the file", max_steps=3)
    await loop.run(context)
    assert context.status == "success", context.reason
    assert compactor.compact.await_count == 1
    assert requests[1][0]["content"] == "Task summary"


# 功能：新运行从账本恢复上一轮真实用量并触发压缩，模型或上下文改变时不用旧锚点。
# 设计：短文本配大模型用量模拟估算偏差，重建 Store 与 Loop，验证首次真实请求前完成压缩。
@pytest.mark.parametrize("changed", ["same", "model", "history"])
async def test_restored_usage_triggers_compaction(tmp_path: Path, changed: str) -> None:
    route = {"model": "test-model", "wire_format": "anthropic_messages", "route_id": "test"}
    store = SessionStore(tmp_path / "sessions")
    store.append_message("sess-usage", "user", "First question")
    provider = MagicMock()
    provider.context_window = 100000
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn", text="First answer",
        usage=UsageStats(input_tokens=90000, output_tokens=10, context_pct=0.9),
    ))
    first = ExecutionContext(run_id="first", goal="First question", max_steps=2)
    await AgentLoop(
        provider, ToolRegistry(), EventBus(), request_metadata=route,
        transcript=SessionTranscriptSink(store, "sess-usage", "first"),
    ).run(first)
    assert first.status == "success", first.reason
    reopened = SessionStore(tmp_path / "sessions")
    history = reopened.read_messages("sess-usage")
    assert "usage_anchor" not in str(history)
    assert any(event.type == "context.usage_anchor"
               for event in reopened.read_session_events("sess-usage"))
    target = dict(route)
    if changed == "model":
        target["model"] = "another-model"
    if changed == "history":
        history = [{"role": "user", "content": "A different branch"}]
    context = ExecutionContext(run_id="second", goal="Continue", max_steps=2)
    context.messages = history + [{"role": "user", "content": "Continue"}]
    compactor = MagicMock()
    compactor.reserve_tokens = 1000

    # 替换为无费用摘要，后续从 Provider 收到的入参断言压缩确实生效。
    async def compact(ctx, *_args, **_kwargs):
        ctx.messages = [{"role": "user", "content": "Restored summary"}]
        return object()

    compactor.compact = AsyncMock(side_effect=compact)
    provider.chat.reset_mock()
    await AgentLoop(
        provider, ToolRegistry(), EventBus(), request_metadata=target,
        compactor=compactor, compact_threshold=0.8,
        transcript=SessionTranscriptSink(reopened, "sess-usage", "second"),
    ).run(context)
    assert context.status == "success", context.reason
    assert compactor.compact.await_count == int(changed == "same")
    sent = provider.chat.call_args.kwargs["messages"]
    assert (sent[0]["content"] == "Restored summary") == (changed == "same")


# 功能：验证首次请求前压缩长历史，显式关闭自动压缩时不擅自调用摘要。
# 设计：拦截真正循环发送的 Provider 入参，区分“调用过压缩”与“模型收到压缩后历史”。
@pytest.mark.parametrize("enabled", [True, False])
async def test_first_request_compacts_restored_history(enabled: bool) -> None:
    provider = MagicMock()
    provider.context_window = 8000
    requests = []

    # 保存实际模型请求，防止后续上下文变更影响断言。
    async def chat(messages, *_args, **_kwargs):
        requests.append(deepcopy(messages))
        return LlmResponse(stop_reason="end_turn", text="Answer")

    provider.chat = AsyncMock(side_effect=chat)
    compactor = MagicMock()
    compactor.reserve_tokens = 1000

    # 以确定性摘要代替有费用的模型压缩。
    async def compact(context, *_args, **_kwargs):
        context.messages = [{"role": "user", "content": "Restored summary"}]
        return object()

    compactor.compact = AsyncMock(side_effect=compact)
    context = ExecutionContext(run_id="restored", goal="long history " * 4000, max_steps=2)
    loop = AgentLoop(provider, ToolRegistry(), EventBus(), compactor=compactor,
                     compact_threshold=0.8 if enabled else 0)
    await loop.run(context)
    assert context.status == "success"
    assert requests[0][0]["content"] == ("Restored summary" if enabled else context.goal)
    assert compactor.compact.await_count == int(enabled)

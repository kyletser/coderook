from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_rook.core.compact.compactor import Compactor
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.registry import ToolRegistry

# --- stubs -------------------------------------------------------------------


class _MockProvider:
    """Returns canned responses in order; raises exc immediately if given."""

    def __init__(
        self,
        responses: list[LlmResponse],
        exc: BaseException | None = None,
    ) -> None:
        self._responses = iter(responses)
        self._exc = exc

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        thinking: str | None = None,
    ) -> LlmResponse:
        if self._exc is not None:
            raise self._exc
        return next(self._responses)


class _EchoTool(BaseTool):
    name = "echo"
    description = "Echoes msg"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=str(params["msg"]))


class _FailTool(BaseTool):
    name = "fail"
    description = "Always raises"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raise RuntimeError("tool error")


class _BlockingTool(BaseTool):
    name = "blocking"
    description = "Waits until cancelled"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self._started.set()
        await asyncio.Event().wait()
        return ToolResult(content="unreachable")


# --- helpers -----------------------------------------------------------------


def _ctx(max_steps: int = 5) -> ExecutionContext:
    return ExecutionContext(run_id="r1", goal="test goal", max_steps=max_steps)


def _tc(name: str = "echo", inp: dict[str, object] | None = None, uid: str = "t1") -> ToolCallBlock:
    return ToolCallBlock(id=uid, name=name, input=inp or {"msg": "hi"})


def _make_loop(
    provider: _MockProvider,
    registry: ToolRegistry | None = None,
    bus: EventBus | None = None,
) -> tuple[AgentLoop, EventBus]:
    b = bus or EventBus()
    return AgentLoop(provider, registry or ToolRegistry(), b), b  # type: ignore[arg-type]


# 功能：冻结的 Provider 与模型身份直接进入 System Prompt，简单身份问题无需调用工具。
# 设计：只传入非敏感请求元数据并检查权威声明，不依赖 Provider 网络响应。
def test_render_system_includes_authoritative_runtime_identity() -> None:
    provider = _MockProvider([])
    loop = AgentLoop(  # type: ignore[arg-type]
        provider,
        ToolRegistry(),
        EventBus(),
        request_metadata={
            "route_id": "aliyun",
            "model": "qwen3.8-flash",
            "thinking": "high",
        },
    )

    prompt = loop._render_system(_ctx())

    assert "Provider route: aliyun" in prompt
    assert "Model: qwen3.8-flash" in prompt
    assert "Thinking level: high" in prompt
    assert "do not inspect files or run a command" in prompt


async def _events(bus: EventBus) -> list[BaseModel]:
    collected: list[BaseModel] = []

    async def _h(e: BaseModel) -> None:
        collected.append(e)

    bus.subscribe(_h)
    return collected


# --- tests -------------------------------------------------------------------


# 功能：验证 LLM 返回 end_turn 时 loop 将 context 标记为 success
# 设计：单步 provider 直接返回 end_turn，最简正常路径，确认 loop 的基本终止逻辑
async def test_end_turn_marks_success() -> None:
    provider = _MockProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 1


# 功能：验证 end_turn 同时到达的用户纠偏会阻止旧答案结束，并在下一轮交给模型
# 设计：provider 首轮排入纠偏后返回旧答案，第二轮检查上下文并返回新答案，覆盖关键竞态
async def test_steering_arriving_during_end_turn_forces_next_decision() -> None:
    bus = EventBus()
    manager = InteractionManager(bus)
    manager.register_run("r1")

    class _SteeringProvider:
        # 初始化模型调用次数和观察到的消息快照
        def __init__(self) -> None:
            self.calls = 0
            self.seen_messages: list[list[dict[str, object]]] = []

        # 首轮在模型响应期间模拟用户纠偏，次轮返回修正后的最终答案
        async def chat(
            self,
            messages: list[dict[str, object]],
            tool_schemas: list[dict[str, object]],
            bus: EventBus,
            run_id: str,
            *,
            step: int = 0,
            system: str | None = None,
            thinking: str | None = None,
        ) -> LlmResponse:
            self.calls += 1
            self.seen_messages.append([dict(message) for message in messages])
            if self.calls == 1:
                assert manager.steer(run_id, "不要删除旧接口")
                return LlmResponse(stop_reason="end_turn", text="旧方案")
            return LlmResponse(stop_reason="end_turn", text="已保留旧接口")

    provider = _SteeringProvider()
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        bus,
        interaction_manager=manager,
    )
    ctx = _ctx()

    await loop.run(ctx)

    assert ctx.status == "success"
    assert ctx.step == 2
    assert ctx.result == "已保留旧接口"
    assert "不要删除旧接口" in str(provider.seen_messages[1][-1]["content"])


# 功能：验证达到 max_steps 时 loop 以 exceeded_max_steps 原因将 context 标记为 failed
# 设计：设置 max_steps=2 + 无限 tool_use provider，同时验证 step 数量和失败原因，确认计数器与终止逻辑联动正确
async def test_max_steps_marks_failed() -> None:
    tc = _tc("unknown", {})
    provider = _MockProvider([LlmResponse(stop_reason="tool_use", tool_calls=[tc])] * 10)
    loop, _ = _make_loop(provider)
    ctx = _ctx(max_steps=2)
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "exceeded_max_steps"
    assert ctx.step == 2


# 功能：验证不限步数的交互循环可超过旧默认上限并正常返回答案
# 设计：使用二十五次不同参数的本地工具调用，排除重复提醒并覆盖真实执行循环
async def test_unbounded_loop_finishes_after_twenty_steps() -> None:
    responses = [
        LlmResponse(stop_reason="tool_use", tool_calls=[
            _tc(inp={"msg": str(step)}, uid=f"call-{step}"),
        ])
        for step in range(25)
    ]
    responses.append(LlmResponse(stop_reason="end_turn", text="done"))
    registry = ToolRegistry()
    registry.register(_EchoTool())
    loop, _ = _make_loop(_MockProvider(responses), registry)
    context = _ctx(max_steps=0)
    await loop.run(context)
    assert context.status == "success"
    assert context.step == 26
    assert context.result == "done"


# 功能：验证不同参数的探索不产生定时或预算催促，硬步数限制仍然有效
# 设计：五步假模型分别完成或耗尽，比较真实请求快照与回放，避免用提示代替完成证据
@pytest.mark.parametrize("finish", [True, False])
async def test_no_periodic_reminder_and_hard_limit_unchanged(
    tmp_path: Path, finish: bool,
) -> None:
    responses = [LlmResponse(stop_reason="tool_use", tool_calls=[
        _tc(inp={"msg": str(step)}, uid=f"call-{step}"),
    ]) for step in range(5)]
    if finish:
        responses[-1] = LlmResponse(stop_reason="end_turn", text="verified result")
    provider = _MockProvider(responses)
    registry = ToolRegistry()
    registry.register(_EchoTool())
    store = SessionStore(tmp_path)
    store.append_message("sess-budget", "user", "test goal")
    transcript = SessionTranscriptSink(store, "sess-budget", "r1")
    loop = AgentLoop(provider, registry, EventBus(), transcript=transcript)  # type: ignore[arg-type]
    ctx = _ctx(max_steps=5)
    await loop.run(ctx)
    reminders = [message for message in store.derive_messages("sess-budget")
                 if str(message.get("content", "")).startswith("Runtime step budget:")]
    assert reminders == []
    assert ctx.step == 5
    assert ctx.status == ("success" if finish else "failed")
    assert ctx.reason == (None if finish else "exceeded_max_steps")
    rows = [json.loads(line) for line in
            (store.session_dir("sess-budget") / "thread.jsonl").read_text(encoding="utf-8").splitlines()]
    snapshots = [row for row in rows if row.get("type") == "llm.request_prepared"]
    assert not any("Runtime step budget:" in json.dumps(row) for row in snapshots)
    assert not any("Runtime progress checkpoint:" in json.dumps(row) for row in snapshots)


# 功能：验证"调工具 → end_turn"的两步路径最终标记为 success
# 设计：provider 返回 [tool_use, end_turn] 序列，注册真实 EchoTool，覆盖最常见的正常工作路径
async def test_tool_use_then_end_turn_marks_success() -> None:
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc()]),
        LlmResponse(stop_reason="end_turn", text="summary"),
    ])
    registry = ToolRegistry()
    registry.register(_EchoTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 2


# 功能：验证工具结果按 Anthropic 格式（tool_result user 消息）追加到消息历史
# 设计：检查 messages[2]（tool_result 所在位置），断言 tool_use_id 和 content，确认 loop 正确调用了 context.add_tool_result
async def test_tool_result_appended_to_context() -> None:
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc(inp={"msg": "hello"})]),
        LlmResponse(stop_reason="end_turn", text="done"),
    ])
    registry = ToolRegistry()
    registry.register(_EchoTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)
    # messages: [goal, assistant(tool_use), user(tool_result), assistant(end_turn)]
    tool_result_msg = ctx.messages[2]
    assert tool_result_msg["role"] == "user"
    block = tool_result_msg["content"][0]  # type: ignore[index]
    assert block["tool_use_id"] == "t1"
    assert block["content"] == "hello"


# 功能：验证工具失败时 loop 不终止，而是将错误追加上下文让 LLM 重新决策
# 设计：工具始终 raise + provider 第二步返回 end_turn，确认 loop 最终到达 success；这是 agent 区别于普通脚本的核心特性
async def test_tool_failure_loop_continues_to_success() -> None:
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc("fail", {})]),
        LlmResponse(stop_reason="end_turn", text="handled error"),
    ])
    registry = ToolRegistry()
    registry.register(_FailTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 2


# 功能：验证工具失败的错误信息以 is_error=True 追加进上下文，让 LLM 能感知工具调用失败
# 设计：检查 tool_result block 中的 is_error 标记，与 test_tool_failure_loop_continues_to_success 互补
async def test_tool_failure_result_is_error_in_context() -> None:
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc("fail", {})]),
        LlmResponse(stop_reason="end_turn", text="done"),
    ])
    registry = ToolRegistry()
    registry.register(_FailTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)
    tool_result_msg = ctx.messages[2]
    block = tool_result_msg["content"][0]  # type: ignore[index]
    assert block.get("is_error") is True


# 功能：验证收到 CancelledError 时 loop 将 context 标记为 cancelled 后继续上抛 CancelledError
# 设计：用 pytest.raises 捕获 CancelledError，同时检查 context.status，确认优雅退出行为：先记录状态，再传播取消信号
async def test_cancelled_error_marks_failed_and_reraises() -> None:
    provider = _MockProvider([], exc=asyncio.CancelledError())
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    with pytest.raises(asyncio.CancelledError):
        await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "cancelled"


# 功能：验证 Tool Use 在底层工具尚未完成时已经写入统一 v2 事实日志
# 设计：阻塞真实工具执行并读取 llm.message payload，证明执行前不再依赖 legacy block 行
async def test_tool_use_is_persisted_before_tool_finishes(tmp_path: Path) -> None:
    started = asyncio.Event()
    tool_call = _tc("blocking", {})
    provider = _MockProvider([LlmResponse(stop_reason="tool_use", tool_calls=[tool_call])])
    registry = ToolRegistry()
    registry.register(_BlockingTool(started))
    store = SessionStore(tmp_path)
    transcript = SessionTranscriptSink(store, "sess-1", "r1")
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        registry,
        EventBus(),
        transcript=transcript,
    )
    context = _ctx()

    task = asyncio.create_task(loop.run(context))
    await started.wait()
    rows = [
        json.loads(line)
        for line in (store.session_dir("sess-1") / "thread.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [
        row["payload"]["block"]["type"]
        for row in rows
        if row.get("kind") == "event"
        and row.get("type") == "llm.message"
        and "block" in row.get("payload", {})
    ] == ["tool_use"]
    assert any(
        row.get("kind") == "event" and row.get("type") == "llm.message"
        for row in rows
    )

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# 功能：验证 LLM 调用异常被捕获并标记为 llm_error，不向上传播
# 设计：provider 抛 RuntimeError，确认 loop 不崩溃、context 状态为 failed/llm_error，异常被正确吸收
async def test_llm_api_error_marks_failed() -> None:
    provider = _MockProvider([], exc=RuntimeError("api error"))
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "llm_error"


# 功能：验证每个步骤都发布 step.started 和 step.finished 事件
# 设计：注入 bus + 事件收集器，检查事件类型集合，确认步骤级事件的可观测性（S2 TUI 依赖这两个事件显示进度）
async def test_step_started_and_finished_events_published() -> None:
    bus = EventBus()
    events = await _events(bus)
    provider = _MockProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, _ = _make_loop(provider, bus=bus)
    ctx = _ctx()
    await loop.run(ctx)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "step.started" in types
    assert "step.finished" in types


# 功能：验证 Provider 异常退出时仍为已开始步骤发布唯一的 step.finished
# 设计：让模型调用立即抛错并比较 started/finished 的步骤序号，覆盖正常尾部无法到达的失败路径
async def test_failed_step_still_publishes_matching_finished_event() -> None:
    bus = EventBus()
    events = await _events(bus)
    provider = _MockProvider([], exc=RuntimeError("api error"))
    loop, _ = _make_loop(provider, bus=bus)

    await loop.run(_ctx())

    started = [event.step for event in events if event.type == "step.started"]  # type: ignore[attr-defined]
    finished = [event.step for event in events if event.type == "step.finished"]  # type: ignore[attr-defined]
    assert started == [1]
    assert finished == started


# 功能：验证完整 assistant 消息先于工具执行持久化，并保留最终回答正文
# 设计：不再断言旧意图标签，直接检查模型实际文本、工具调用和终态消息顺序
async def test_agent_decision_event_precedes_tool_execution() -> None:
    bus = EventBus()
    events = await _events(bus)
    provider = _MockProvider([
        LlmResponse(
            stop_reason="tool_use",
            text="我先检查相关文件。",
            tool_calls=[_tc("read_file", {"path": "README.md"})],
        ),
        LlmResponse(stop_reason="end_turn", text="检查完成。"),
    ])
    loop, _ = _make_loop(provider, bus=bus)

    await loop.run(_ctx())

    types = [event.type for event in events]  # type: ignore[attr-defined]
    messages = [event.model_dump() for event in events if event.type == "agent.message"]  # type: ignore[attr-defined]
    completed = [message for message in messages if message["phase"] == "end"]
    assert completed[0]["content"][0]["text"] == "我先检查相关文件。"
    assert completed[0]["content"][1]["name"] == "read_file"
    assert completed[-1]["content"][0]["text"] == "检查完成。"
    assert types.index("agent.message") < types.index("tool.call_started")


# 功能：验证模型省略文本时仍保留结构化工具调用而不生成虚构进度摘要
# 设计：使用未知执行工具避免依赖具体实现，断言 execute 分类和工具名回退而非空白
async def test_agent_decision_event_has_tool_fallback_without_text() -> None:
    bus = EventBus()
    events = await _events(bus)
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc("custom_tool", {})]),
        LlmResponse(stop_reason="end_turn", text="done"),
    ])
    loop, _ = _make_loop(provider, bus=bus)

    context = _ctx()
    context.goal = "请执行这个任务"
    await loop.run(context)

    messages = [event.model_dump() for event in events if event.type == "agent.message"]  # type: ignore[attr-defined]
    first = next(message for message in messages if message["phase"] == "end")
    assert first["content"][0]["name"] == "custom_tool"
    assert not any(block["type"] == "text" for block in first["content"])


# 功能：验证模型请求的工具名称和 action 参数完整保留在消息中
# 设计：覆盖读写和控制类 action，确保移植没有重新按意图标签改写模型工具请求
@pytest.mark.parametrize(
    ("tool_name", "params", "expected"),
    [
        ("memory", {"action": "search", "query": "runtime"}, "inspect"),
        ("memory", {"action": "save", "content": "fact"}, "change"),
        ("tasks", {"action": "update", "task_id": "t1"}, "plan"),
        ("update_plan", {"plan": []}, "plan"),
    ],
)
async def test_agent_decision_classifies_action_family_calls(
    tool_name: str,
    params: dict[str, object],
    expected: str,
) -> None:
    bus = EventBus()
    events = await _events(bus)
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc(tool_name, params)]),
        LlmResponse(stop_reason="end_turn", text="done"),
    ])
    loop, _ = _make_loop(provider, bus=bus)

    await loop.run(_ctx())

    messages = [event.model_dump() for event in events if event.type == "agent.message"]  # type: ignore[attr-defined]
    message = next(item for item in messages if item["phase"] == "end")
    assert message["content"][0]["name"] == tool_name
    assert message["content"][0]["arguments"] == params


# 功能：验证多步执行后 step 计数器正确累积到步数总量
# 设计：三步序列 [tool_use, tool_use, end_turn]，确认 step==3，排除计数器初始化错误或某步未递增的情况
async def test_step_counter_increments_across_steps() -> None:
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc()]),
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc()]),
        LlmResponse(stop_reason="end_turn", text="done"),
    ])
    registry = ToolRegistry()
    registry.register(_EchoTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx(max_steps=10)
    await loop.run(ctx)
    assert ctx.step == 3
    assert ctx.status == "success"


# 功能：验证 LLM 文本响应以正确的 content block 格式追加到消息历史
# 设计：检查 messages[1] 的 role 和 content block 结构，确认 loop 构造的 assistant 消息符合 Anthropic 格式
async def test_assistant_message_blocks_added_to_context() -> None:
    provider = _MockProvider([LlmResponse(stop_reason="end_turn", text="answer")])
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    await loop.run(ctx)
    assistant_msg = ctx.messages[1]
    assert assistant_msg["role"] == "assistant"
    blocks = assistant_msg["content"]
    assert blocks[0]["type"] == "text"  # type: ignore[index]
    assert blocks[0]["text"] == "answer"  # type: ignore[index]


# 功能：验证上下文超限时 loop 自动压缩一次并继续完成原任务
# 设计：序列 provider 依次抛超限、返回摘要、返回最终答案，精确覆盖 reactive compact 恢复链
async def test_context_overflow_compacts_and_recovers(tmp_path: Path) -> None:
    class _SequenceProvider:
        # 初始化三阶段调用计数器
        def __init__(self) -> None:
            self.calls = 0

        # 按调用阶段模拟超限、压缩摘要和恢复后的最终响应
        async def chat(
            self,
            messages: list[dict[str, object]],
            tool_schemas: list[dict[str, object]],
            bus: EventBus,
            run_id: str,
            *,
            step: int = 0,
            system: str | None = None,
            thinking: str | None = None,
        ) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("context_length_exceeded")
            if self.calls == 2:
                return LlmResponse(
                    stop_reason="end_turn",
                    text=(
                        '{"goal":"recover","completed":[],"constraints":[],'
                        '"decisions":[],"files":[],"todos":[],"errors":[],'
                        '"critical_data":[]}'
                    ),
                )
            return LlmResponse(stop_reason="end_turn", text="recovered")

    provider = _SequenceProvider()
    bus = EventBus()
    loop = AgentLoop(
        provider,
        ToolRegistry(),
        bus,
        compactor=Compactor(bus, tmp_path, "sess-1", keep_recent_tokens=80),
    )
    context = _ctx()
    context.messages = [
        {"role": "user", "content": "Previous task " + "x" * 4000},
        {"role": "assistant", "content": "Previous work " + "y" * 400},
        {"role": "user", "content": "Continue current task " + "z" * 500},
    ]

    await loop.run(context)

    assert context.status == "success"
    assert context.result == "recovered"
    assert provider.calls == 3


# 功能：未知 stop_reason（如兼容后端的 "stop"）且无工具调用时按 end_turn 成功收尾
# 设计：直接构造 stop_reason="stop" 的响应，断言不空转到 max_steps 而是正常终止
async def test_unknown_stop_reason_without_tools_ends_turn() -> None:
    provider = _MockProvider([LlmResponse(stop_reason="stop", text="done")])
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 1


# 功能：未知 stop_reason 带工具调用时按 tool_use 执行工具
# 设计：首响应 "stop" + echo 调用，次响应 end_turn；断言工具确实被执行且 run 成功
async def test_unknown_stop_reason_with_tools_runs_act_phase() -> None:
    provider = _MockProvider(
        [
            LlmResponse(stop_reason="stop", tool_calls=[_tc()]),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_EchoTool())
    loop, bus = _make_loop(provider, registry)
    events = await _events(bus)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert any(type(e).__name__ == "ToolCallFinishedEvent" for e in events)


# 功能：验证 Pi 式无工具截断保留正文并明确结束为不完整
# 设计：提供额外完整响应作为诱饵，断言不会擅自续写并消耗下一次调用
async def test_length_completion_stops_with_partial_result() -> None:
    class _LengthProvider:
        # 初始化调用次数并记录第二轮输入
        def __init__(self) -> None:
            self.calls = 0
            self.second_messages: list[dict[str, object]] = []

        # 首轮返回截断，第二轮返回完整终态
        async def chat(
            self,
            messages: list[dict[str, object]],
            tool_schemas: list[dict[str, object]],
            bus: EventBus,
            run_id: str,
            **_kwargs: object,
        ) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                return LlmResponse(
                    stop_reason="max_tokens",
                    text="partial",
                    completion_status="length",
                )
            self.second_messages = list(messages)
            return LlmResponse(
                stop_reason="end_turn",
                text="complete",
                completion_status="completed",
            )

    provider = _LengthProvider()
    loop = AgentLoop(provider, ToolRegistry(), EventBus())  # type: ignore[arg-type]
    ctx = _ctx()

    await loop.run(ctx)

    assert ctx.status == "failed" and ctx.reason == "incomplete"
    assert ctx.result == "partial"
    assert provider.calls == 1
    assert not provider.second_messages


# 功能：验证无工具调用的 length 保留部分结果并结束为不完整
# 设计：准备两次响应，确认不自动生成用户未提交的续写消息来消耗第二次响应
async def test_second_length_completion_fails_incomplete() -> None:
    provider = _MockProvider(
        [
            LlmResponse(
                stop_reason="max_tokens",
                text="part-one",
                completion_status="length",
            ),
            LlmResponse(
                stop_reason="max_tokens",
                text="part-two",
                completion_status="length",
            ),
        ]
    )
    loop, _ = _make_loop(provider)
    ctx = _ctx()

    await loop.run(ctx)

    assert ctx.status == "failed"
    assert ctx.reason == "incomplete"
    assert ctx.result == "part-one"


# 功能：验证内容过滤终态直接失败并保留安全可见摘要
# 设计：构造 content_filtered 响应，断言 loop 不进行 no-content 重试且不标记成功
async def test_content_filtered_completion_is_not_success() -> None:
    provider = _MockProvider(
        [
            LlmResponse(
                stop_reason="content_filtered",
                text="response filtered",
                completion_status="content_filtered",
            )
        ]
    )
    loop, _ = _make_loop(provider)
    ctx = _ctx()

    await loop.run(ctx)

    assert ctx.status == "failed"
    assert ctx.reason == "content_filtered"
    assert ctx.result == "response filtered"


class _PermissionRequiredTool(BaseTool):
    name = "deny_fast"
    description = "Returns permission_required error"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="denied", is_error=True, error_type="permission_required")


class _PermissionDeniedMutationTool(BaseTool):
    name = "denied_edit"
    description = "Returns a denied mutation"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    # 返回权限拒绝结果，模拟用户未批准修改工具
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="denied", is_error=True, error_type="permission_denied")


# 功能：验证修改任务的文件操作被拒绝后不会把说明文字误标成任务成功
# 设计：先返回 permission_denied 再让模型解释失败，断言最终结果保留正文但状态为权限失败
async def test_permission_denied_mutation_is_not_reported_as_success() -> None:
    provider = _MockProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_tc("denied_edit", {}, "d1")],
            ),
            LlmResponse(stop_reason="end_turn", text="需要权限后才能完成修改。"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_PermissionDeniedMutationTool())
    loop = AgentLoop(  # type: ignore[arg-type]
        provider,
        registry,
        EventBus(),
        request_metadata={"task_profile": {"risk": "mutate"}},
    )
    ctx = _ctx()

    await loop.run(ctx)

    assert ctx.status == "failed"
    assert ctx.reason == "permission_denied"
    assert ctx.result == "需要权限后才能完成修改。"


# 功能：验证同时需要 Shell 的修改任务被拒绝后仍以失败结束
# 设计：将任务风险设为 shell 并保留 mutation_intent，覆盖风险等级覆盖修改意图时曾出现的假成功
async def test_permission_denied_shell_mutation_is_not_reported_as_success() -> None:
    provider = _MockProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_tc("denied_edit", {}, "d1")],
            ),
            LlmResponse(stop_reason="end_turn", text="命令和修改权限被拒绝。"),
        ]
    )
    registry = ToolRegistry()
    registry.register(_PermissionDeniedMutationTool())
    loop = AgentLoop(  # type: ignore[arg-type]
        provider,
        registry,
        EventBus(),
        request_metadata={
            "task_profile": {
                "risk": "shell",
                "signals": ["mutation_intent", "shell_intent"],
            }
        },
    )
    ctx = _ctx()

    await loop.run(ctx)

    assert ctx.status == "failed"
    assert ctx.reason == "permission_denied"
    assert ctx.result == "命令和修改权限被拒绝。"


# 功能：act 阶段因 permission_required 提前终止时为后续工具补合成 tool_result
# 设计：同批两个工具，首个返回 permission_required；断言 run 失败且第二个 tool_use 也有配对结果，无孤儿
async def test_aborted_act_phase_fills_skipped_tool_results() -> None:
    provider = _MockProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_tc("deny_fast", {}, "d1"), _tc("echo", {"msg": "x"}, "e1")],
            ),
        ]
    )
    registry = ToolRegistry()
    registry.register(_PermissionRequiredTool())
    registry.register(_EchoTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "failed"
    tool_result_ids = [
        block.get("tool_use_id")
        for message in ctx.messages
        if message.get("role") == "user"
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert "d1" in tool_result_ids
    assert "e1" in tool_result_ids

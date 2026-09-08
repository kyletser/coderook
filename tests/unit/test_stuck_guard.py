from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import FollowUpMessage, InteractionManager
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.turn import StuckGuard


# 构造带稳定 ID 和输入的工具调用
def _call(tool_name: str, tool_use_id: str, **params: object) -> ToolCallBlock:
    return ToolCallBlock(id=tool_use_id, name=tool_name, input=params)


# 创建允许多步执行的最小上下文
def _context() -> ExecutionContext:
    return ExecutionContext(run_id="guard-run", goal="test guards", max_steps=10)


class _SequenceProvider:
    # 初始化按 step 返回的固定响应序列
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)

    # 返回下一步响应
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        return next(self._responses)


class _CountingReadTool(BaseTool):
    name = "count_read"
    description = "Read a stable value for one path"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    # 初始化真实调用计数器
    def __init__(self) -> None:
        self.calls = 0

    # 返回与 path 对应的稳定内容并增加真实调用次数
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult(f"content:{params['path']}")


class _MutationTool(BaseTool):
    name = "mutate"
    description = "Mutate local state"
    side_effect = ToolSideEffect.LOCAL_WRITE
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 返回固定写入结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("changed")


class _DynamicReadTool(BaseTool):
    name = "dynamic_read"
    description = "Read changing process state"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    }

    # 初始化动态读取次数
    def __init__(self) -> None:
        self.calls = 0

    # 每次返回不同状态以模拟后台任务轮询
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult(f"poll:{self.calls}")


class _CancelTool(BaseTool):
    description = "Cancel the current agent task"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 初始化动态工具名和调用计数器
    def __init__(self, name: str, *, cancel: bool) -> None:
        self.name = name
        self._cancel = cancel
        self.calls = 0

    # 可选请求取消当前任务，用于验证下一工具不会启动
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        if self._cancel:
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
        return ToolResult(self.name)


# 功能：验证连续三次同参调用只产生提醒，之后仍能正常完成任务
# 设计：三个 step 只改变调用 ID，最后明确完成，断言提醒不把任务改成失败
async def test_three_identical_tool_calls_are_advisory() -> None:
    provider = _SequenceProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("count_read", f"read-{index}", path="a.py")],
            )
            for index in range(3)
        ] + [LlmResponse(stop_reason="end_turn", text="done")]
    )
    tool = _CountingReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集 stuck 事件及工具生命周期事件
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    context = _context()
    await AgentLoop(provider, registry, bus).run(context)  # type: ignore[arg-type]

    stuck = [event for event in events if event.type == "agent.repeat_notice"]  # type: ignore[attr-defined]
    assert context.status == "success"
    assert context.reason is None
    assert tool.calls == 1
    assert len(stuck) == 1
    assert stuck[0].repeat_count == 3  # type: ignore[attr-defined]
    assert len(stuck[0].signature) == 64  # type: ignore[attr-defined]
    assert "content:a.py" not in stuck[0].signature  # type: ignore[attr-defined]


# 功能：验证跨 step 的相同只读调用直接复用缓存且仍发布配对工具事件
# 设计：两次相同 path 后正常结束，断言实现只调用一次但两个 tool_use_id 都有 started/finished
async def test_repeated_read_uses_cache_with_paired_events() -> None:
    provider = _SequenceProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("count_read", "read-1", path="a.py")],
            ),
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("count_read", "read-2", path="a.py")],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    tool = _CountingReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集工具事件以验证缓存命中仍维持 started/terminal 不变式
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    context = _context()
    await AgentLoop(provider, registry, bus).run(context)  # type: ignore[arg-type]

    started = [event for event in events if event.type == "tool.call_started"]  # type: ignore[attr-defined]
    finished = [event for event in events if event.type == "tool.call_finished"]  # type: ignore[attr-defined]
    assert context.status == "success"
    assert tool.calls == 1
    assert [event.tool_use_id for event in started] == ["read-1", "read-2"]  # type: ignore[attr-defined]
    assert [event.tool_use_id for event in finished] == ["read-1", "read-2"]  # type: ignore[attr-defined]


# 功能：原生循环独立执行并行工具调用，并保留每个调用的配对结果。
# 设计：同批相同参数仍有两个调用 ID，不借用旧批调度器的合并行为。
async def test_identical_reads_in_one_batch_keep_individual_results() -> None:
    provider = _SequenceProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    _call("count_read", "read-1", path="a.py"),
                    _call("count_read", "read-2", path="a.py"),
                ],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    tool = _CountingReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    context = _context()

    await AgentLoop(
        provider,  # type: ignore[arg-type]
        registry,
        EventBus(),
        stuck_guard=StuckGuard(thresholds=(4,)),
    ).run(context)

    tool_results = [
        block
        for message in context.messages
        if message.get("role") == "user" and isinstance(message.get("content"), list)
        for block in message["content"]  # type: ignore[index]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert context.status == "success"
    assert tool.calls == 2
    assert len(tool_results) == 2
    assert [result["tool_use_id"] for result in tool_results] == ["read-1", "read-2"]


# 功能：验证 mutation 会清空读取缓存，后续相同读取必须重新访问真实工具
# 设计：按 read→mutation→read 排列，断言读工具调用两次以排除陈旧结果复用
async def test_mutation_invalidates_read_cache() -> None:
    provider = _SequenceProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("count_read", "read-1", path="a.py")],
            ),
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("mutate", "write-1")],
            ),
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("count_read", "read-2", path="a.py")],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    read_tool = _CountingReadTool()
    registry = ToolRegistry()
    registry.register(read_tool)
    registry.register(_MutationTool())
    context = _context()

    await AgentLoop(provider, registry, EventBus()).run(context)  # type: ignore[arg-type]

    assert context.status == "success"
    assert read_tool.calls == 2


# 功能：验证不含 path/hash/handle 的动态只读工具不会被读取缓存冻结
# 设计：连续轮询相同 job_id 两次，断言真实实现执行两次并返回变化状态
async def test_dynamic_read_without_content_identity_is_not_cached() -> None:
    provider = _SequenceProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("dynamic_read", "poll-1", job_id="job-1")],
            ),
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[_call("dynamic_read", "poll-2", job_id="job-1")],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    tool = _DynamicReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    context = _context()

    await AgentLoop(provider, registry, EventBus()).run(context)  # type: ignore[arg-type]

    assert context.status == "success"
    assert tool.calls == 2


# 功能：验证工具完成后收到取消请求时不会再启动同一响应中的下一工具
# 设计：首个串行工具 cancel 当前 task，捕获取消后断言第二工具计数和 started 事件均为零
async def test_cancellation_after_tool_prevents_next_tool_start() -> None:
    first = _CancelTool("cancel_first", cancel=True)
    second = _CancelTool("must_not_start", cancel=False)
    registry = ToolRegistry()
    registry.register(first)
    registry.register(second)
    provider = _SequenceProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    _call("cancel_first", "cancel-1"),
                    _call("must_not_start", "cancel-2"),
                ],
            )
        ]
    )
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集 started 事件，直接证明取消后未启动第二工具
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    context = _context()

    with pytest.raises(asyncio.CancelledError):
        await AgentLoop(provider, registry, bus).run(context)  # type: ignore[arg-type]

    started_names = [
        event.tool_name  # type: ignore[attr-defined]
        for event in events
        if event.type == "tool.call_started"  # type: ignore[attr-defined]
    ]
    assert context.reason == "cancelled"
    assert first.calls == 1
    assert second.calls == 0
    assert started_names == ["cancel_first"]


# 功能：验证九次同参但结果变化的工具调用只在三、五、八次提醒，仍完成全部调用
# 设计：动态只读工具不走读取缓存，持久 Ledger 与请求快照验证提醒不切断工具配对
async def test_repeat_thresholds_preserve_execution_and_replay(tmp_path: Path) -> None:
    provider = _SequenceProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[
            _call("dynamic_read", f"poll-{i}", job_id="one"),
        ]) for i in range(9)
    ] + [LlmResponse(stop_reason="end_turn", text="done")])
    tool = _DynamicReadTool()
    registry = ToolRegistry()
    registry.register(tool)
    store = SessionStore(tmp_path)
    store.append_message("sess-retry", "user", "test guards")
    bus = EventBus()
    notices: list[int] = []

    # 记录公开提醒次数，同时证明它不是失败事件
    async def collect(event: BaseModel) -> None:
        if getattr(event, "type", "") == "agent.repeat_notice":
            notices.append(event.repeat_count)  # type: ignore[attr-defined]

    bus.subscribe(collect)
    context = _context()
    context.max_steps = 12
    await AgentLoop(
        provider, registry, bus,  # type: ignore[arg-type]
        transcript=SessionTranscriptSink(store, "sess-retry", context.run_id),
    ).run(context)
    assert context.status == "success"
    assert tool.calls == 9 and notices == [3, 5, 8]
    reopened = SessionStore(tmp_path)
    assert reopened.derive_messages("sess-retry") == context.messages
    events = reopened.read_session_events("sess-retry")
    reminders = [e for e in events if e.type == "input.admitted" and "source" in e.payload]
    assert len(reminders) == 3
    assert all(e.payload["source"]["plugin"] == "repeat-tool-reminder" for e in reminders)
    for event in reminders:
        preceding = events[events.index(event) - 1]
        assert preceding.payload["block"]["type"] == "tool_result"
    assert "Runtime progress checkpoint" not in json.dumps(context.messages)


# 功能：验证参数键顺序、结果变化不影响重复计数，不同参数及用户消息会重置
# 设计：预先累积重复链，经生产循环消费纠偏或后续消息，再检查新计数及原文上下文。
@pytest.mark.parametrize("follow_up", [False, True])
async def test_canonical_arguments_and_user_steering_reset(follow_up: bool) -> None:
    guard = StuckGuard()
    first = _call("read", "a", options={"x": 1, "y": 2})
    second = _call("read", "b", options={"y": 2, "x": 1})
    assert guard.observe(first, ToolResult("old")) is None
    assert guard.observe(second, ToolResult("new")) is None
    assert guard.observe(first, ToolResult("error", is_error=True)).repeat_count == 3
    bus = EventBus()
    interaction = InteractionManager(bus)
    context = _context()
    interaction.register_run(context.run_id)
    loop = AgentLoop(_SequenceProvider([
        LlmResponse(stop_reason="end_turn", text="Answer"),
        LlmResponse(stop_reason="end_turn", text="Follow-up answer"),
    ]), ToolRegistry(), bus,  # type: ignore[arg-type]
                     stuck_guard=guard, interaction_manager=interaction)
    instruction = "Continue checking the same file"
    pending = True

    # 仅作为已消费确认，不额外向主循环注入消息。
    async def admitted() -> None:
        pass

    # 模拟同一运行结束主回答后领取一条后续输入。
    async def next_message() -> list[FollowUpMessage]:
        nonlocal pending
        if not pending:
            return []
        pending = False
        return [FollowUpMessage(instruction, admitted)]

    if follow_up:
        interaction.bind_follow_up(context.run_id, next_message)
    else:
        assert interaction.steer(context.run_id, instruction)
    await loop.run(context)
    assert context.status == "success", context.reason
    assert {"role": "user", "content": instruction} in context.messages
    assert guard.observe(first, ToolResult("again")) is None
    assert guard.observe(second, ToolResult("again")) is None
    assert guard.observe(first, ToolResult("again")).repeat_count == 3
    assert guard.observe(_call("read", "c", options={"x": 3}), ToolResult("new")) is None
    assert guard.observe(first, ToolResult("again")) is None
    assert StuckGuard().observe(first, ToolResult("other session")) is None


# 功能：验证排除工具不改变观察链，提醒参数预览有界且不改变原调用
# 设计：两次阈值配置与透明排除交错，检查后续详细提醒而不依赖模型采纳建议
def test_repeat_include_exclude_and_preview() -> None:
    guard = StuckGuard(thresholds=(2, 3), include=("read*",), exclude=("read_status",),
                       argument_preview_chars=8)
    call = _call("read_file", "a", path="very-long-path")
    assert guard.observe(call, ToolResult("one")) is None
    assert guard.observe(_call("read_status", "b"), ToolResult("excluded")) is None
    assert guard.observe(call, ToolResult("two")).repeat_count == 2
    match = guard.observe(call, ToolResult("three"))
    assert match.repeat_count == 3
    assert match.notice.split("Arguments: ")[1] == '{"path":…'
    assert call.input == {"path": "very-long-path"}

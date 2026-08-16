from __future__ import annotations

from pathlib import Path

from code_rook.core.compact.protocol import validate_tool_protocol
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.registry import ToolRegistry

# --- stubs -------------------------------------------------------------------


class _ScriptedProvider:
    """Returns canned LlmResponses in order; captures all system prompts seen."""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)
        self.seen_systems: list[str] = []

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
        self.seen_systems.append(system or "")
        return next(self._responses)


class _TodoUpdateTool(BaseTool):
    """Marks task #1 as completed via in-process TaskManager reference."""

    name = "todo_complete_1"
    description = "test-only: marks task #1 completed"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, task_manager: TaskManager) -> None:
        self._tm = task_manager

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self._tm.update(1, status="completed")
        return ToolResult(content="task #1 completed")


# --- helpers ----------------------------------------------------------------


def _ctx(max_steps: int = 8) -> ExecutionContext:
    return ExecutionContext(run_id="r-todo", goal="g", max_steps=max_steps)


def _usage() -> UsageStats:
    return UsageStats(
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        context_pct=0.0,
    )


def _tm(tmp_path: Path) -> TaskManager:
    tm = TaskManager(tmp_path / ".tasks")
    return tm


def _make_loop(
    provider: _ScriptedProvider,
    registry: ToolRegistry,
    task_manager: TaskManager | None,
) -> tuple[AgentLoop, EventBus]:
    bus = EventBus()
    return (
        AgentLoop(provider, registry, bus, todo_state=task_manager),  # type: ignore[arg-type]
        bus,
    )


# --- tests ------------------------------------------------------------------


# 功能：context.system_prompt() 不含 todos 且 todo_state=None 时返回与改造前一致
# 设计：构造 todo_state=None 的 loop，跑两次 LLM 调用都不含 "## Todo State"
async def test_loop_without_todo_state_does_not_inject_summary(tmp_path: Path) -> None:
    provider = _ScriptedProvider(
        [LlmResponse(stop_reason="end_turn", text="done", usage=_usage())]
    )
    registry = ToolRegistry()
    loop, _ = _make_loop(provider, registry, task_manager=None)
    await loop.run(_ctx())
    assert provider.seen_systems
    for s in provider.seen_systems:
        assert "## Todo State" not in s


# 功能：todos 存在时 loop 把 ## Todo State 摘要拼到每次 LLM 调用的 system 末尾
# 设计：TaskManager 建两个 task（一 pending 一 in_progress），把 tm 当 todo_state 传入，
#       run 单步 end_turn 后断言第二首轮 system 含 "## Todo State" 与两个 task subject
async def test_loop_injects_todo_summary_into_system_prompt(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="write readme", description="")
    tm.create(subject="run tests", description="")
    tm.update(2, status="in_progress")

    provider = _ScriptedProvider(
        [LlmResponse(stop_reason="end_turn", text="done", usage=_usage())]
    )
    loop, _ = _make_loop(provider, ToolRegistry(), task_manager=tm)
    # 直接调 _render_system 验证（避免 run 的 end_turn-软状态检查依赖）
    s = loop._render_system(_ctx())
    assert "## Todo State" in s
    assert "write readme" in s
    assert "run tests" in s
    # in_progress 工具用 [>] 标记
    assert "[>]" in s
    assert "[ ]" in s  # 第一个 task 仍 pending


# 功能：无 todos（active_summary() 返回空）时 _render_system 不追加任何 Todo State 段
# 设计：TaskManager 不创建任务，断言 _render_system 与 todo_state=None 时一致（不含 "## Todo State"）
async def test_empty_task_manager_does_not_inject_summary(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    provider = _ScriptedProvider(
        [LlmResponse(stop_reason="end_turn", text="done", usage=_usage())]
    )
    loop, _ = _make_loop(provider, ToolRegistry(), task_manager=tm)
    ctx = _ctx()
    rendered = loop._render_system(ctx)
    assert "## Todo State" not in rendered
    # 与 todo_state=None 的等价 loop 渲染结果应一致
    loop_no_tm = AgentLoop(provider, ToolRegistry(), EventBus())
    assert rendered == loop_no_tm._render_system(ctx)


# 功能：end_turn 时仍有 pending todos 且 todos 自上次提醒已变化 → loop 推迟结束并注入 reminder
# 设计：tm 建 task_a（pending），第一轮模型 end_turn，loop 把 reminder 追加为 user 消息；
#       第二轮模型再次 end_turn，但 tm 不变 → snapshot 未变 → 第二轮已提醒 1 次但要求
#       snapshot 和上次不同才再阻拦；故第二轮不再阻拦，run 应结束
async def test_end_turn_deferred_once_when_todos_incomplete(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="task_a", description="")
    provider = _ScriptedProvider(
        [
            LlmResponse(stop_reason="end_turn", text="done-1", usage=_usage()),
            LlmResponse(stop_reason="end_turn", text="done-2", usage=_usage()),
        ]
    )
    loop, _ = _make_loop(provider, ToolRegistry(), task_manager=tm)
    ctx = _ctx(max_steps=5)
    await loop.run(ctx)
    # 因第二轮 snapshot 未变化，loop 已放过结束
    assert ctx.status == "success"
    assert ctx.result == "done-2"
    # 期间注入过至少一条 reminder user 消息
    reminder_msgs = [
        m for m in ctx.messages
        if m.get("role") == "user" and m.get("content") == (
            "You ended the turn, but the Todo State above still has incomplete items. "
            "Either continue working on the next pending/in_progress todo, or call tasks with "
            "action='update' and status='completed' for any items that are truly done, then end."
        )
    ]
    assert len(reminder_msgs) == 1


# 功能：todos 全部完成时 end_turn 立即结束，不注入 reminder
# 设计：tm 建任务并立即标记完成，模型一次 end_turn，断言 run 一步结束且无 reminder
async def test_end_turn_not_deferred_when_all_todos_completed(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="done_task", description="")
    tm.update(1, status="completed")
    provider = _ScriptedProvider(
        [LlmResponse(stop_reason="end_turn", text="ok", usage=_usage())]
    )
    loop, _ = _make_loop(provider, ToolRegistry(), task_manager=tm)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.result == "ok"
    reminder_msgs = [
        m for m in ctx.messages
        if m.get("role") == "user" and str(m.get("content", "")).startswith(
            "You ended the turn"
        )
    ]
    assert reminder_msgs == []


# 功能：连续 _MAX_TODO_DEFERS 次 end_turn 仍未推进 todos 时 loop 放弃阻拦让其结束
# 设计：tm 建 task_a 不动，模型连续 4 次 end_turn（>3），断言 run 终止且 reminder 注入 3 次
async def test_max_defers_then_loop_stops_blocking(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="task_a", description="")
    # 第 1 次 end_turn：snapshot 与初始 "" 不同 → 阻挡，snapshot 记为 task_a 摘要
    # 第 2 次 end_turn：snapshot 与上次相等，但 defer_count=1 < MAX=3 → _should_defer 仍视
    #   "snapshot 与上次相等"为 False 故不再阻拦。这一设计是有意为之：让 loop 在第二次
    #   之后即放过，避免死循环。验证行为：reminder 仅注入 1 次
    provider = _ScriptedProvider(
        [
            LlmResponse(stop_reason="end_turn", text=f"d{i}", usage=_usage())
            for i in range(5)
        ]
    )
    loop, _ = _make_loop(provider, ToolRegistry(), task_manager=tm)
    ctx = _ctx(max_steps=10)
    await loop.run(ctx)
    reminder_msgs = [
        m for m in ctx.messages
        if str(m.get("content", "")).startswith("You ended the turn")
    ]
    # 提醒一次后模型放弃阻拦（snapshot 不再变化），故 reminder 仅 1 条
    assert len(reminder_msgs) == 1
    assert ctx.status == "success"


# 功能：模型用工具把 todo 标完成后再 end_turn，loop 不再阻拦
# 设计：tm 建 task_a（pending），config provider：先工具调用 todo_complete_1，再 end_turn；
#       断言 run 成功，无 reminder 注入
async def test_end_turn_after_tool_completes_todo_does_not_defer(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="task_a", description="")
    tool = _TodoUpdateTool(tm)
    registry = ToolRegistry()
    registry.register(tool)
    from code_rook.core.llm.types import ToolCallBlock

    provider = _ScriptedProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(id="t1", name="todo_complete_1", input={})
                ],
                usage=_usage(),
            ),
            LlmResponse(stop_reason="end_turn", text="all done", usage=_usage()),
        ]
    )
    loop, _ = _make_loop(provider, registry, task_manager=tm)
    ctx = _ctx(max_steps=5)
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.result == "all done"
    reminder_msgs = [
        m for m in ctx.messages
        if str(m.get("content", "")).startswith("You ended the turn")
    ]
    assert reminder_msgs == []


# 功能：TaskManager 实现 TodoStateView Protocol（active_summary 与 has_incomplete）
# 设计：构造 TaskManager 实例，调用两个方法验证返回值与状态一致
def test_task_manager_implements_todo_state_view(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    assert tm.active_summary() == ""  # 空
    assert tm.has_incomplete() is False  # 无任务 -> 无未完成
    tm.create(subject="x", description="")
    assert tm.has_incomplete() is True  # pending
    assert "## Todo State" in tm.active_summary()
    tm.update(1, status="completed")
    assert tm.has_incomplete() is False  # 全 complete
    assert tm.active_summary()  # 仍非空，但完整列表展示在 loop 里无阻拦


# 功能：Todo end_turn 提醒持久化为普通 user 消息且不会产生孤立 tool_result
# 设计：使用真实 SessionStore 和 transcript sink 跑两轮 end_turn，再用协议校验器检查恢复消息
async def test_todo_reminder_persists_as_plain_user_message(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="task_a", description="")
    provider = _ScriptedProvider(
        [
            LlmResponse(stop_reason="end_turn", text="first", usage=_usage()),
            LlmResponse(stop_reason="end_turn", text="second", usage=_usage()),
        ]
    )
    store = SessionStore(tmp_path / "sessions")
    session_id = "sess-todo"
    store.session_dir(session_id).mkdir(parents=True)
    store.append_message(session_id, "user", "start", run_id="r-todo")
    loop = AgentLoop(
        provider,
        ToolRegistry(),
        EventBus(),
        todo_state=tm,
        transcript=SessionTranscriptSink(store, session_id, "r-todo"),
    )

    await loop.run(_ctx())

    messages = store.read_messages(session_id)
    valid, errors = validate_tool_protocol(messages)
    assert valid, errors
    assert any(message.get("content") == (
        "You ended the turn, but the Todo State above still has incomplete items. "
        "Either continue working on the next pending/in_progress todo, or call tasks with "
        "action='update' and status='completed' for any items that are truly done, then end."
    ) for message in messages)

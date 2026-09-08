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


# 功能：仅显式启用 tasks 工具时提供任务板状态，默认工具集不注入不可操作的任务板。
# 设计：同一任务板分别绑定空工具集和 tasks 工具，比较实际系统提示中的条目。
async def test_loop_injects_todo_summary_into_system_prompt(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="write readme", description="")
    tm.create(subject="run tests", description="")
    tm.update(2, status="in_progress")

    provider = _ScriptedProvider(
        [LlmResponse(stop_reason="end_turn", text="done", usage=_usage())]
    )
    loop, _ = _make_loop(provider, ToolRegistry(), task_manager=tm)
    assert "## Todo State" not in loop._render_system(_ctx())
    registry = ToolRegistry()
    task_tool = _TodoUpdateTool(tm)
    task_tool.name = "tasks"
    registry.register(task_tool)
    loop, _ = _make_loop(provider, registry, task_manager=tm)
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


# 功能：存在未完成任务板条目时仍允许模型正常回答，不伪造用户消息迫使续跑。
# 设计：提供两份回答，断言只消费第一份，且原始任务条目不被偷偷标为完成。
async def test_end_turn_with_pending_todos_does_not_force_continuation(tmp_path: Path) -> None:
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
    assert ctx.status == "success"
    assert ctx.result == "done-1"
    assert len(provider.seen_systems) == 1
    assert tm.has_incomplete()
    assert [m for m in ctx.messages if m.get("role") == "user"] == [
        {"role": "user", "content": ctx.goal}
    ]


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


# 功能：更高步数上限不等于强制模型消耗额外步骤。
# 设计：准备五次响应和未完成任务，确认原生循环首次正常结束就停止调用模型。
async def test_step_budget_does_not_force_extra_answers(tmp_path: Path) -> None:
    tm = _tm(tmp_path)
    tm.create(subject="task_a", description="")
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
    assert reminder_msgs == []
    assert len(provider.seen_systems) == 1
    assert ctx.result == "d0"
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


# 功能：任务板不会在会话历史中伪造用户指令，最终回答能够直接持久恢复。
# 设计：真实 SessionStore 配合预备的第二份回答，确认只保存首次结果及原始用户输入。
async def test_todo_state_does_not_fabricate_persisted_user_messages(tmp_path: Path) -> None:
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
    assert [message for message in messages if message["role"] == "user"] == [
        {"role": "user", "content": "start"}
    ]
    assert messages[-1]["content"] == [{"type": "text", "text": "first"}]
    assert len(provider.seen_systems) == 1

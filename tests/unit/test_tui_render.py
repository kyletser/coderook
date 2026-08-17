# 针对 tui/render.py 事件族渲染函数的单元测试：用简化的假 App 校验各分支副作用
"""构建一个脱离 Textual 事件循环的最小人造 App，喂入事件后断言渲染副作用。"""

from __future__ import annotations

from typing import Any

from textual.css.query import NoMatches
from textual.widgets import Static

from code_rook.tui.render import render_event
from code_rook.tui.widgets.permission import PermissionBlock, PermissionSelect
from code_rook.tui.widgets.stream import LLMStreamBlock, ToolCallBlock, ToolStepGroup


# 一个最小 ToolStepGroup 占位，避免测试直接依赖真实控件构造
class _FakeStepGroup:
    pass


# 简化假 App：记录 append/header/mount 等渲染副作用，实现 render_event 依赖的最小成员
class _FakeApp:
    def __init__(self) -> None:
        self._appended: list[Any] = []
        self._header_calls: list[str] = []
        self._current_llm: LLMStreamBlock | None = None
        self._subagent_run_ids: dict[str, str] = {}
        self._subagent_start_times: dict[str, float] = {}
        self._current_steps: dict[str, int] = {}
        self._tool_step_groups: dict[tuple[str, int], ToolStepGroup] = {}
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._active_run_id: str | None = None
        self._cancel_requested = False
        self._cancel_armed = False
        self._busy = False
        self._last_context_pct = 0.0
        self._header_state = "connecting"
        self._route = ""
        self._model = ""
        self._plan_review_pending = False
        self._plan_session_id: str | None = None
        self._plan_request = ""
        self._pending_question_id: str | None = None
        self._answering_question = False
        self.focused: Any = None
        self._mounted_selects: list[PermissionSelect] = []
        self._mounted_count = 0
        self._maybe_autotitled = False

    def _append(self, widget: Any) -> None:
        self._appended.append(widget)

    def _break_llm(self) -> None:
        self._current_llm = None

    def _accumulate_cost(self, event: dict[str, Any]) -> None:
        pass

    def _update_header(self, state: str) -> None:
        self._header_state = state
        self._header_calls.append(state)

    def _clear_user_question(self) -> None:
        self._pending_question_id = None
        self._answering_question = False

    def _prompt(self) -> None:
        return None

    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self._mounted_selects.append(select)
        self._mounted_count += 1

    def query_one(self, *args: Any, **kwargs: Any) -> Any:
        raise NoMatches() from None

    def mount(self, *args: Any, **kwargs: Any) -> None:
        self._mounted_count += 1

    def _maybe_autotitle_session(self) -> None:
        self._maybe_autotitled = True


# 新建一个默认配置的假 App
def _new_app() -> _FakeApp:
    return _FakeApp()


# 提取 Static/日志类控件渲染的纯文本内容，供断言比对
def _render_text(widget: Any) -> str:
    for name in ("_Static__content", "_content"):
        content = getattr(widget, name, None)
        if isinstance(content, str):
            return content
    return str(widget)


# 功能：验证 llm.token 事件会新建流式块并追加 token
# 设计：用假 App 记录 _append 调用与 _current_llm；断言追加对象即 active 流块，避免序列化干扰
def test_llm_token_creates_and_appends_stream_block() -> None:
    app = _new_app()

    render_event(app, {"type": "llm.token", "token": "Hello", "run_id": "r"})

    assert app._current_llm is not None
    assert app._current_llm.text == "Hello"
    assert app._appended == [app._current_llm]


# 功能：验证同一个 llm.token 事件会分块累积到同一流式块
# 设计：连续两次喂入 token，断言不新建块、文本拼接，验证 LLM 前段不因中途 break 换块
def test_llm_token_accumulates_into_same_block() -> None:
    app = _new_app()

    render_event(app, {"type": "llm.token", "token": "Hello", "run_id": "r"})
    render_event(app, {"type": "llm.token", "token": " world", "run_id": "r"})

    assert app._current_llm is not None
    assert app._current_llm.text == "Hello world"
    assert len(app._appended) == 1


# 功能：验证 agent.stuck 事件渲染包含工具名与重复次数的日志行
# 设计：检查 _append 收集到的 Static 控件文本包含 tool_name，验证 stage/tail 渲染文案
def test_agent_stuck_logs_tool_name() -> None:
    app = _new_app()

    render_event(
        app,
        {"type": "agent.stuck", "tool_name": "grep", "repeat_count": 3},
    )

    texts = [_render_text(w) for w in app._appended if isinstance(w, Static)]
    assert any("grep" in t and "identical" in t for t in texts)


# 功能：验证 session.waiting_for_input 复位 busy 并把顶栏切回 ready
# 设计：先置 busy=True，再喂事件，断言 busy 复位、header 变更已记录的 ready 状态
def test_session_waiting_for_input_resets_ready() -> None:
    app = _new_app()
    app._busy = True

    render_event(app, {"type": "session.waiting_for_input"})

    assert app._busy is False
    assert app._cancel_requested is False
    assert app._header_state == "ready"
    assert "ready" in app._header_calls


# 功能：验证 plan.updated 事件把计划明细渲染进日志
# 设计：构造带 in_progress 步骤的计划事件，断言 Append 出来的 Static 文本包含 Plan 标题与步骤
def test_plan_updated_renders_plan_lines() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "plan.updated",
            "explanation": "重构渲染层",
            "plan": [
                {"status": "completed", "step": "抽取 render.py"},
                {"status": "in_progress", "step": "拆分事件分支"},
            ],
        },
    )

    texts = [_render_text(w) for w in app._appended if isinstance(w, Static)]
    joined = "\n".join(texts)
    assert "Plan updated" in joined
    assert "重构渲染层" in joined
    assert "[>] 拆分事件分支" in joined
    assert "[x] 抽取 render.py" in joined


# 功能：验证 user_question.asked 记录待处理问题并切换顶栏状态
# 设计：匹配会话 id 后断言 _pending_question_id 被记录且 header 变为 question
def test_user_question_asks_and_switches_header() -> None:
    app = _new_app()
    app._session_id = "s1"

    render_event(
        app,
        {
            "type": "user_question.asked",
            "session_id": "s1",
            "question_id": "q-1",
            "question": "是否继续?",
            "header": "确认",
            "options": ["yes", "no"],
        },
    )

    assert app._pending_question_id == "q-1"
    assert app._answering_question is False
    assert app._header_state == "question"


# 功能：验证 run.finished 失败时渲染含原因的结果行并清理步骤索引
# 设计：用非 success 状态触发 else 分支，断言 x 标记文本与 steps 计数、步骤索引被清空
def test_run_finished_failure_renders_result_and_cleans_steps() -> None:
    app = _new_app()
    app._current_steps = {"run-1": 3}
    app._tool_step_groups = {("run-1", 3): _FakeStepGroup()}

    render_event(
        app,
        {"type": "run.finished", "status": "error", "steps": 3, "reason": "boom", "run_id": "run-1"},
    )

    texts = [_render_text(w) for w in app._appended if isinstance(w, Static)]
    joined = "\n".join(texts)
    assert "Failed after 3 steps" in joined and "boom" in joined
    assert app._active_run_id is None
    assert "run-1" not in app._current_steps
    assert not app._tool_step_groups


# 功能：验证 subagent.started 记录子代理并渲染起始行
# 设计：断言子代理登记且 append 的日志包含描述文本
def test_subagent_started_tracks_and_logs() -> None:
    app = _new_app()

    render_event(
        app,
        {"type": "subagent.started", "run_id": "child-12345678", "description": "researcher"},
    )

    assert app._subagent_run_ids["child-12345678"] == "researcher"
    texts = [_render_text(w) for w in app._appended if isinstance(w, Static)]
    assert any("researcher" in t for t in texts)


# 功能：验证 tool.call_started 把工具块登记进待处理映射并按 step 分组
# 设计：喂事件后断言 _pending_tool_blocks 与 _tool_step_groups 均被写入，工具块可回填结果
def test_tool_call_tracks_block_and_group() -> None:
    app = _new_app()
    app._current_steps = {"run-1": 2}

    render_event(
        app,
        {
            "type": "tool.call_started",
            "tool_use_id": "t-1",
            "tool_name": "grep",
            "params": {"pattern": "x"},
            "run_id": "run-1",
        },
    )

    assert "t-1" in app._pending_tool_blocks
    assert ("run-1", 2) in app._tool_step_groups
    assert app._pending_tool_blocks["t-1"]._tool_name == "grep"


# 功能：验证 tool.call_finished 把结果回填到已登记的工具块
# 设计：先喂 started 再用同一 tool_use_id 喂 finished，断言块被移出 pending 且结果已写入
def test_tool_call_finished_fills_result() -> None:
    app = _new_app()
    app._current_steps = {"run-1": 0}

    render_event(
        app,
        {
            "type": "tool.call_started",
            "tool_use_id": "t-1",
            "tool_name": "grep",
            "params": {},
            "run_id": "run-1",
        },
    )
    render_event(
        app,
        {"type": "tool.call_finished", "tool_use_id": "t-1", "elapsed_ms": 5, "output": "hit"},
    )

    assert "t-1" not in app._pending_tool_blocks
    block = app._tool_step_groups[("run-1", 0)]._blocks[0]
    assert block._output == "hit"
    assert block._finished


# 功能：验证 context.compacted 渲染摘要行
# 设计：喂压缩事件，断言日志包含 Context compacted 标题，验证 misc 族分支
def test_context_compacted_logs_summary() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "context.compacted",
            "original_tokens": 100,
            "compacted_tokens": 50,
            "summary_tokens": 10,
            "retained_messages": 2,
            "retained_tokens": 8,
            "quality_score": 0.9,
            "trigger": "manual",
            "summary_path": "",
        },
    )

    texts = [_render_text(w) for w in app._appended if isinstance(w, Static)]
    assert any("Context compacted" in t for t in texts)


# 功能：验证 permission.requested 登记审批卡并向假 App 挂载选择控件
# 设计：断言 pending map 被写入、挂载助手被调用、prompt 关注状态未触发崩溃
def test_permission_requested_registers_block() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "permission.requested",
            "tool_use_id": "p-1",
            "tool_name": "bash",
            "param_preview": "ls",
            "params": {"command": "ls"},
        },
    )

    assert "p-1" in app._pending_permission_blocks
    assert app._mounted_count == 1


# 功能：验证 log.line 把日志行渲染进日志区
# 设计：喂 WARNING 级日志事件，断言 append 出包含 source 与 message 的 Static 文本
def test_log_line_renders_message() -> None:
    app = _new_app()

    render_event(
        app,
        {"type": "log.line", "level": "WARNING", "source": "core", "message": "disk full"},
    )

    texts = [_render_text(w) for w in app._appended if isinstance(w, Static)]
    assert any("WARNING" in t and "core" in t and "disk full" in t for t in texts)
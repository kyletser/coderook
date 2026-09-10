# 针对 tui/render.py 事件族渲染函数的单元测试：用简化的假 App 校验各分支副作用
"""构建一个脱离 Textual 事件循环的最小人造 App，喂入事件后断言渲染副作用。"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from textual.css.query import NoMatches
from textual.widgets import Markdown, Static

from code_rook.tui.render import render_event
from code_rook.tui.widgets.permission import PermissionBlock, PermissionSelect
from code_rook.tui.widgets.stream import LLMStreamBlock, ToolCallBlock, ToolStepGroup


# 一个最小 ToolStepGroup 占位，避免测试直接依赖真实控件构造
class _FakeStepGroup:
    pass


# 功能：工具详情面板展示扩展结构化数据而不要求将其拼入工具文本
# 设计：直接调用真实控件的详情生成函数，覆盖 JSON 序列化和中文标签
def test_tool_structured_details() -> None:
    block = ToolCallBlock("extension", {}, locale="zh-CN", presentation={
        "details": {"files": ["example.py"], "count": 1},
    })
    detail = block._detail_text()
    assert "结构化详情" in detail
    assert '"count": 1' in detail
    assert "example.py" in detail


# 功能：阶段变化更新状态栏，不再生成重复的固定话术时间线。
# 设计：连续发送多个真实阶段事件，确认状态可见但未追加虚构的助手说明。
def test_phase_updates_header_without_timeline_messages() -> None:
    app = _FakeApp()
    for phase in ("understanding", "exploring", "executing"):
        render_event(app, {
            "type": "run.phase_changed", "run_id": "test", "phase": phase,
            "current": 1, "total": 8, "summary": "Fixed phase explanation",
        })
    assert app._header_calls == ["understanding", "exploring", "executing"]
    assert app._appended == []


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
        self._plan_run_id: str | None = None
        self._plan_request = ""
        self._pending_question_id: str | None = None
        self._answering_question = False
        self.focused: Any = None
        self._mounted_selects: list[PermissionSelect] = []
        self._mounted_count = 0
        self._maybe_autotitled = False
        self._ready_restores = 0

    def _append(self, widget: Any) -> None:
        self._appended.append(widget)

    # 将控件插到已存在的兄弟控件之前，模拟真实时间线的顺序调整
    def _insert_before(self, widget: Any, sibling: Any) -> None:
        self._appended.insert(self._appended.index(sibling), widget)

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

    # 清除测试 App 当前匹配的计划审阅状态
    def _clear_plan_review(self) -> None:
        self._plan_review_pending = False
        self._plan_session_id = None
        self._plan_run_id = None
        self._plan_request = ""

    # 记录计划解决事件触发了输入框恢复
    def _restore_ready_prompt(self) -> None:
        self._ready_restores += 1

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


# 功能：验证正文先流出时，后到的思考仍排列在最终回答之前且不会折叠回答
# 设计：以同一消息的更新和完成快照驱动真实控件，断言语义顺序、折叠状态和块去重
def test_pi_message_preserves_visible_answer() -> None:
    app = _new_app()
    event = {
        "type": "agent.message", "run_id": "pi-run", "message_id": "message-1",
        "phase": "update", "content": [{"type": "text", "text": "partial"}],
    }
    render_event(app, event)
    event.update(phase="end", content=[
        {"type": "text", "text": "Final answer"},
        {"type": "thinking", "thinking": "Internal analysis"},
    ])
    render_event(app, event)
    assert len(app._appended) == 2
    thinking, answer = app._appended
    assert answer.text == "Final answer"
    assert "answer" in answer.classes
    assert "collapsed" not in answer.classes
    assert "collapsed" in thinking.classes


# 功能：成功运行缺少流式消息时仍将 run.finished 的权威结果显示为最终回答。
# 设计：直接发送仅含 result_summary 的完成事件，断言生成普通 Markdown 而非空完成卡。
def test_run_finished_uses_result_summary_as_answer_fallback() -> None:
    app = _new_app()

    render_event(app, {
        "type": "run.finished", "run_id": "fallback-run", "status": "success",
        "steps": 1, "result_summary": "这是最终回答。",
    })

    assert len(app._appended) == 1
    assert isinstance(app._appended[0], Markdown)
    assert app._last_assistant_text == "这是最终回答。"
    assert app._maybe_autotitled


# 功能：只有思考块的原生消息不能冒充已经展示过的最终回答。
# 设计：先结束一条纯 thinking 消息再完成运行，验证 result_summary 仍进入时间线。
def test_thinking_only_message_does_not_hide_result_fallback() -> None:
    app = _new_app()
    render_event(app, {
        "type": "agent.message", "run_id": "thinking-run", "message_id": "m",
        "role": "assistant", "phase": "end",
        "content": [{"type": "thinking", "thinking": "分析中"}],
    })
    render_event(app, {
        "type": "run.finished", "run_id": "thinking-run", "status": "success",
        "steps": 1, "result_summary": "最终结果",
    })

    answers = [widget for widget in app._appended if isinstance(widget, Markdown)]
    assert len(answers) == 1
    assert app._last_assistant_text == "最终结果"


# 功能：验证先思考后正文且最终快照重排时，正文仍展开且思考不重复。
# 设计：故意改变内容数组的位置，防止用数组索引误把回答绑定到折叠的思考控件。
def test_message_thinking_before_answer_remains_visible() -> None:
    app = _new_app()
    event = {"type": "agent.message", "run_id": "r", "message_id": "m", "phase": "update",
             "content": [{"type": "thinking", "thinking": "Thinking"}]}
    render_event(app, event)
    event["content"] = [{"type": "text", "text": "Answer"},
                        {"type": "thinking", "thinking": "Thinking"}]
    render_event(app, event)
    event["phase"] = "end"
    render_event(app, event)
    assert len(app._appended) == 2
    thinking, answer = app._appended
    assert "collapsed" in thinking.classes
    assert "answer" in answer.classes and "collapsed" not in answer.classes
    assert answer.text == "Answer"
    assert app._last_assistant_text == "Answer"


# 提取 Static/日志类控件渲染的纯文本内容，供断言比对
def _render_text(widget: Any) -> str:
    for name in ("_Static__content", "_content"):
        content = getattr(widget, name, None)
        if isinstance(content, str):
            return content
    return str(widget)


# 功能：可见扩展消息带来源且回放不重复，不覆盖复制最终回答的内容
# 设计：连续两次渲染同一持久消息，检查控件数量和最后回答字段保持不变
def test_custom_message_replay_keeps_answer() -> None:
    app = _new_app()
    app._last_assistant_text = "Original answer"
    event = {"type": "agent.message", "run_id": "r", "message_id": "r:extension:0",
             "role": "custom", "custom_type": "notice", "phase": "end",
             "content": [{"type": "text", "text": "Custom note"}]}
    render_event(app, event)
    render_event(app, event)
    assert len(app._appended) == 1
    assert app._appended[0].text == "Extension · notice\n\nCustom note"
    assert app._last_assistant_text == "Original answer"


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


# 功能：验证失败尝试清除本次临时正文，下一次流式输出不与半条回答拼接
# 设计：控件替身记录 remove，另一个 run 的失败不影响当前文本块
def test_failed_attempt_clears_only_matching_live_response() -> None:
    app = _new_app()
    block = MagicMock()
    app._current_llm = block
    app._current_llm_run_id = "run"
    render_event(app, {"type": "llm.attempt_finished", "run_id": "other", "status": "failed"})
    assert app._current_llm is block
    render_event(app, {"type": "llm.attempt_finished", "run_id": "run", "status": "failed"})
    block.remove.assert_called_once()
    assert app._current_llm is None
    render_event(app, {"type": "llm.token", "run_id": "run", "token": "recovered"})
    assert app._current_llm.text == "recovered"


# 功能：验证重试等待与重复提醒有可读展示，但提醒不会复位运行状态
# 设计：直接渲染持久事件，检查等待秒数和非阻断提示，不启动真实终端
def test_retry_delay_and_repeat_notice_are_visible() -> None:
    app = _new_app()
    app._locale = "en-US"
    app._busy = True
    render_event(app, {"type": "llm.retry", "kind": "transient",
                       "attempt": 2, "delay_ms": 1500})
    render_event(app, {"type": "agent.repeat_notice", "tool_name": "read", "repeat_count": 5})
    texts = [_render_text(w) for w in app._appended]
    assert any("1.5s" in text for text in texts)
    assert any("execution continues" in text for text in texts)
    assert app._busy


# 功能：验证 agent.stuck 事件渲染包含工具名与重复次数的日志行
# 设计：检查 _append 收集到的 Static 控件文本包含 tool_name，验证 stage/tail 渲染文案
def test_agent_stuck_logs_tool_name() -> None:
    app = _new_app()
    app._locale = "en-US"

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


# 功能：验证当前会话匹配 run 的 live plan.resolved 会立即清除审阅面板状态
# 设计：直接投递 durable 解决事件并记录 prompt 恢复，另用不匹配 run 证明不会误清新计划
def test_plan_resolved_clears_only_matching_review() -> None:
    app = _new_app()
    app._session_id = "sess-plan"
    app._plan_session_id = "sess-plan"
    app._plan_run_id = "run-new"
    app._plan_review_pending = True

    render_event(
        app,
        {
            "type": "plan.resolved",
            "session_id": "sess-plan",
            "run_id": "run-old",
            "decision": "cancel",
        },
    )
    assert app._plan_review_pending

    render_event(
        app,
        {
            "type": "plan.resolved",
            "session_id": "sess-plan",
            "run_id": "run-new",
            "decision": "approve",
        },
    )

    assert not app._plan_review_pending
    assert app._plan_run_id is None
    assert app._ready_restores == 1
    assert app._header_state == "ready"


# 功能：验证 repository context 与 working set 以路径和预算摘要展示而非原始 JSON
# 设计：连续投递两个上下文事件，检查文件、字符预算和缓存统计都能在日志中直接阅读
def test_repository_context_and_working_set_render_as_product_summary() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "context.repository",
            "paths": ["src/auth.py", "tests/test_auth.py"],
            "used_chars": 4200,
            "budget_chars": 12000,
            "cache_hits": 8,
            "parsed_files": 2,
        },
    )
    render_event(
        app,
        {
            "type": "context.working_set",
            "paths": ["src/auth.py"],
        },
    )

    joined = "\n".join(
        _render_text(widget) for widget in app._appended if isinstance(widget, Static)
    )
    assert "Repository context" in joined
    assert "4200/12000" in joined
    assert "cache=8 parsed=2" in joined
    assert "Working set" in joined
    assert "src/auth.py" in joined


# 功能：验证结构化验证事件显示通过数、门禁名称、工作集和失败类别
# 设计：分别投递 pass 与 fail 事件，确认用户无需打开 receipt 即可分辨验证终态和失败原因
def test_verification_events_render_pass_and_failure_details() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "verification.completed",
            "action": "run_tests",
            "passed": 2,
            "failed": 0,
            "paths": ["src/auth.py"],
            "gates": [{"name": "pytest"}, {"name": "ruff"}],
        },
    )
    render_event(
        app,
        {
            "type": "verification.failed",
            "action": "run_verifiers",
            "passed": 1,
            "failed": 1,
            "failure_class": "test_failure",
            "paths": ["src/auth.py"],
            "gates": [{"name": "mypy"}],
        },
    )

    joined = "\n".join(
        _render_text(widget) for widget in app._appended if isinstance(widget, Static)
    )
    assert "Verification passed" in joined
    assert "2 passed / 0 failed" in joined
    assert "pytest, ruff" in joined
    assert "Verification failed" in joined
    assert "test_failure" in joined
    assert "mypy" in joined


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
    app._locale = "en-US"
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


# 功能：验证模型调用失败时 TUI 直接给出路由诊断与修复入口
# 设计：投递标准 llm_error 终态并检查同一失败块含 doctor/provider/config 指引，避免用户只看到内部原因码
def test_run_finished_model_failure_renders_recovery_guidance() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "run.finished",
            "status": "failed",
            "steps": 1,
            "reason": "llm_error",
            "run_id": "run-model",
        },
    )

    joined = "\n".join(
        _render_text(widget) for widget in app._appended if isinstance(widget, Static)
    )
    assert "llm_error" in joined
    assert "/doctor" in joined
    assert "/provider" in joined
    assert "/config" in joined


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


# 功能：验证 Goal 继续决策展示轮次、预算、墙钟与需要确认的恢复入口
# 设计：直接渲染暂停决策事件并断言审计字段和 `/goal resume` 同时出现在一张状态卡中
def test_goal_continue_decision_renders_bounded_status() -> None:
    app = _new_app()

    render_event(
        app,
        {
            "type": "goal.continue_decision",
            "should_continue": False,
            "reason": "max_auto_turns_reached",
            "auto_turns_used": 3,
            "remaining_auto_turns": 0,
            "tokens_used": 900,
            "token_budget": 1000,
            "wall_elapsed_seconds": 120,
            "max_wall_seconds": 1800,
            "paused_needs_confirmation": True,
        },
    )

    texts = [_render_text(widget) for widget in app._appended if isinstance(widget, Static)]
    rendered = "\n".join(texts)
    assert "Goal 已暂停" in rendered
    assert "auto=3/3" in rendered
    assert "tokens=900/1000" in rendered
    assert "wall=120s/1800s" in rendered
    assert "/goal resume" in rendered


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

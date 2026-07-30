from __future__ import annotations

from rich.markdown import Markdown
from rich.markup import render
from textual.app import App, ComposeResult
from textual.widget import Widget

from code_rook.tui.app import (
    CodeRookTuiApp,
    LLMStreamBlock,
    ModelPicker,
    PermissionBlock,
    PermissionSelect,
    SessionPicker,
    ToolCallBlock,
    _param_summary,
    _preview,
)


# 功能：验证权限审批面板以紧凑层级展示工具、请求、选项和决策说明
# 设计：渲染 Rich markup 后检查可见文本，避免样式标签掩盖内容回归
def test_permission_panel_shows_request_context_and_choices() -> None:
    panel = PermissionSelect(
        "tool-1",
        "bash",
        "command='git status'",
        {"command": "git status --short"},
    )

    plain = render(panel._render_ui()).plain

    assert "bash" in plain
    assert "CodeRook wants to run a shell command" in plain
    assert "COMMAND" in plain
    assert "git status --short" in plain
    assert "Allow once" in plain
    assert "Always allow" in plain
    assert "Deny" in plain
    assert "Always deny" in plain
    assert "this request only" in plain
    assert "remember for future sessions" in plain
    assert "navigate" in plain


# 功能：验证权限请求文本中的 Rich 标记会按普通文本显示
# 设计：使用带方括号的参数预览，确保请求内容不会注入终端样式
def test_permission_panel_escapes_request_markup() -> None:
    panel = PermissionSelect("tool-1", "bash", "[bold]literal[/bold]")

    plain = render(panel._render_ui()).plain

    assert "[bold]literal[/bold]" in plain


def test_permission_panel_shows_full_bash_command() -> None:
    command = "printf 'this command is deliberately longer than sixty characters'"
    panel = PermissionSelect(
        "tool-1",
        "bash",
        "command='printf …'",
        {"command": command},
    )

    assert command in render(panel._render_ui()).plain


# 功能：验证待审批摘要与决策结果文案保持一致
# 设计：直接检查纯文本生成结果，不依赖挂载 App 或 IPC
def test_permission_block_uses_pending_and_decision_labels() -> None:
    block = PermissionBlock("tool-1", "bash", "command='git status'")

    assert "approval required" in block._pending_text()
    assert "permission-pending" in block.classes
    assert PermissionBlock.LABEL_MAP["allow_once"] == "allowed once"
    assert PermissionBlock.LABEL_MAP["deny_once"] == "denied"

    block._resolve("allow_once")

    assert "permission-pending" not in block.classes


async def test_permission_panel_keyboard_navigation_and_escape() -> None:
    class PermissionHarness(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.decisions: list[str] = []

        def compose(self) -> ComposeResult:
            yield PermissionSelect("tool-1", "bash", "command='git status'")

        def on_permission_select_decided(self, message: PermissionSelect.Decided) -> None:
            self.decisions.append(message.decision)

    app = PermissionHarness()
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        panel = app.query_one(PermissionSelect)
        assert panel.has_focus
        assert panel.border_title is None

        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.decisions == ["always_allow"]

        await pilot.press("escape")
        await pilot.pause()
        assert app.decisions == ["always_allow", "deny_once"]


async def test_session_picker_renders_and_selects_saved_session() -> None:
    sessions = [
        {
            "session_id": "sess-current",
            "mode": "chat",
            "status": "waiting_for_input",
            "title": "Current work",
        },
        {
            "session_id": "sess-older",
            "mode": "chat",
            "status": "closed",
            "title": "Older work",
        },
    ]

    class PickerHarness(App[None]):
        def __init__(self) -> None:
            super().__init__()
            self.selected: list[str] = []

        def compose(self) -> ComposeResult:
            yield SessionPicker(sessions, "sess-current")

        def on_session_picker_selected(self, message: SessionPicker.Selected) -> None:
            self.selected.append(message.session_id)

    app = PickerHarness()
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        picker = app.query_one(SessionPicker)
        plain = render(picker._render_ui()).plain
        assert "Current work" in plain
        assert "Older work" in plain
        assert "current" in plain

        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.selected == ["sess-older"]


# 功能：验证模型选择器标记当前模型并可用键盘切换到下一项
# 设计：在最小 Textual App 中真实发送按键，覆盖渲染、焦点和 Selected 消息链路
async def test_model_picker_renders_and_selects_model() -> None:
    models = ["claude-sonnet-4-6", "claude-opus-4-6"]

    class PickerHarness(App[None]):
        # 初始化测试宿主并收集模型选择结果
        def __init__(self) -> None:
            super().__init__()
            self.selected: list[str] = []

        # 挂载待测模型选择器
        def compose(self) -> ComposeResult:
            yield ModelPicker(models, "claude-sonnet-4-6")

        # 接收模型选择消息并记录模型 ID
        def on_model_picker_selected(self, message: ModelPicker.Selected) -> None:
            self.selected.append(message.model)

    app = PickerHarness()
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        picker = app.query_one(ModelPicker)
        plain = render(picker._render_ui()).plain
        assert "claude-sonnet-4-6" in plain
        assert "current" in plain
        assert "/model add <model-id>" in plain

        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.selected == ["claude-opus-4-6"]


# 功能：验证 TUI 内建命令包含模型选择入口
# 设计：直接读取候选列表，避免依赖 socket 连接或完整界面挂载
def test_tui_builtin_commands_include_model_picker() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    items = dict(app._build_slash_items())  # type: ignore[attr-defined]

    assert items["model"] == "show or switch the active model"


def test_tui_builtin_commands_include_session_picker_and_new_session() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    items = dict(app._build_slash_items())  # type: ignore[attr-defined]

    assert items["sessions"] == "open saved session picker"
    assert items["new"] == "start a new chat session"


# 功能：验证 _preview 超出长度时截断并追加省略号
# 设计：不依赖任何 TUI 组件，纯函数测试
def test_preview_truncates() -> None:
    assert _preview("abcde", 3) == "abc…"
    assert _preview("ab", 5) == "ab"


# 功能：验证工具参数摘要优先展示工具最关键字段
# 设计：覆盖 read_file/bash/note_save 三类常见工具，避免工具块摘要退化成整段 JSON
def test_param_summary_prefers_key_fields() -> None:
    assert _param_summary("read_file", {"path": "README.md"}) == "path='README.md'"
    assert _param_summary("bash", {"command": "echo hi", "timeout": 1}) == "command='echo hi'"
    assert _param_summary("note_save", {"content": "Python 3.12"}) == "content='Python 3.12'"


# 功能：验证 llm.token 事件累积到 LLMStreamBlock，不连续 token 各自新开一块
# 设计：monkey-patch _append 收集追加的 widgets，断言 token 追加到同一块；
#       发送非 token 事件后新 block 被重置，下一个 token 开启新块
def test_llm_tokens_accumulate_in_block() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "Hello", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "llm.token", "token": " world", "run_id": "r", "ts": "t"})

    assert len(appended) == 1  # same block reused
    assert isinstance(appended[0], LLMStreamBlock)
    assert appended[0]._text == "Hello world"  # type: ignore[attr-defined]


# 功能：验证 LLMStreamBlock 结束时会把累积文本渲染为 Rich Markdown
# 设计：直接调用 finalize_markdown，断言 renderable 类型，覆盖 Markdown polish 的核心行为
def test_llm_block_finalize_renders_markdown() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\n- one\n\n```python\nprint('hi')\n```")
    block.finalize_markdown()
    assert isinstance(block.content, Markdown)


# 功能：验证非 token 事件后 _current_llm 被重置，下一个 token 开启新块
# 设计：插入 step.started 中断流，验证之前的 block 被 finalize，之后的 llm.token 创建新 LLMStreamBlock
def test_llm_block_resets_after_non_token_event() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "A", "run_id": "r", "ts": "t"})
    app._handle_event({"type": "step.started", "run_id": "r", "step": 2, "ts": "t"})
    app._handle_event({"type": "llm.token", "token": "B", "run_id": "r", "ts": "t"})

    llm_blocks = [w for w in appended if isinstance(w, LLMStreamBlock)]
    assert len(llm_blocks) == 2
    assert llm_blocks[0]._finalized  # type: ignore[attr-defined]


# 功能：验证 run.started 事件追加 Static widget 且包含 run_id 和 goal
# 设计：monkey-patch _append，断言追加的 widget 的 renderable 包含关键字段
def test_run_started_appends_widget_with_content() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.started", "run_id": "run-abc", "goal": "do the thing", "ts": "t"
    })

    assert len(appended) == 1
    rendered = appended[0].content
    assert "run-abc" in rendered
    assert "do the thing" in rendered


# 功能：验证 run.finished success 追加包含 "completed" 的 widget
# 设计：monkey-patch _append，检查 rendered 内容包含 completed 和 green
def test_run_finished_success_shows_completed() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "success", "steps": 3, "ts": "t"
    })

    rendered = appended[0].content
    assert "completed" in rendered
    assert "green" in rendered


# 功能：验证 run.finished failed 追加包含 "failed" 和 red 的 widget
# 设计：与 success 对称，检查颜色标记差异
def test_run_finished_failed_shows_red() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "failed",
        "steps": 1, "reason": "llm_error", "ts": "t"
    })

    rendered = appended[0].content
    assert "failed" in rendered
    assert "red" in rendered


# 功能：验证 tool.call_started 追加 ToolCallBlock，call_finished 更新其结果
# 设计：直接调用 _handle_event 两次，通过 _pending_tool_blocks 验证状态流转
def test_tool_call_started_and_finished() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "tool.call_started",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "params": {"command": "echo hi"},
        "run_id": "r", "ts": "t",
    })
    assert "uid-1" in app._pending_tool_blocks  # type: ignore[attr-defined]

    app._handle_event({
        "type": "tool.call_finished",
        "tool_use_id": "uid-1",
        "tool_name": "bash",
        "elapsed_ms": 42,
        "output": "hi",
        "run_id": "r", "ts": "t",
    })
    assert "uid-1" not in app._pending_tool_blocks  # type: ignore[attr-defined]
    block = appended[0]
    assert isinstance(block, ToolCallBlock)
    assert block._finished  # type: ignore[attr-defined]
    assert block._output == "hi"  # type: ignore[attr-defined]


# 功能：验证 note_save 成功完成时工具块摘要显示 remembered
# 设计：直接操作 ToolCallBlock，覆盖 note_save 的特殊低噪声展示策略
def test_note_save_tool_block_shows_remembered() -> None:
    block = ToolCallBlock("note_save", {"content": "Python 3.12"})
    block.set_result("saved", 3)
    assert "remembered" in block._summary()  # type: ignore[attr-defined]


# 功能：验证提交用户输入时会追加 user turn，并进入 busy 状态
# 设计：用 fake client 替代 SocketClient，直接调用 on_chat_text_area_submitted，
#       覆盖 TextArea 清空内容 + 设置 busy 占位符的核心状态迁移
async def test_input_submit_appends_user_turn_and_disables_prompt() -> None:
    class _FakeArea:
        def __init__(self) -> None:
            self.disabled = False
            self.border_title = ""
            self.text = "hello"

    class _FakeEvent:
        def __init__(self, area: _FakeArea) -> None:
            self.value = area.text
            self.text_area = area

    class _FakeClient:
        async def send_command(self, method: str, params: dict) -> dict:
            return {"run_id": "run-1"}

    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]
    app._update_header = lambda state: None  # type: ignore[method-assign]
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"

    area = _FakeArea()
    event = _FakeEvent(area)
    await app.on_chat_text_area_submitted(event)  # type: ignore[arg-type]

    assert app._busy  # type: ignore[attr-defined]
    assert area.disabled
    assert area.text == ""
    assert "agent is working" in area.border_title.lower()
    assert appended[0].content == "[bold]>[/bold] hello"


async def test_cancel_worker_sends_active_run_id() -> None:
    calls: list[tuple[str, dict]] = []

    class _FakeClient:
        async def send_command(self, method: str, params: dict) -> dict:
            calls.append((method, params))
            return {"run_id": "run-1", "status": "cancelled"}

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _FakeClient()  # type: ignore[assignment]

    await app._do_cancel_run("run-1")  # type: ignore[attr-defined]

    assert calls == [("run.cancel", {"run_id": "run-1"})]


def test_session_interrupted_restores_prompt() -> None:
    class _FakePrompt:
        disabled = True
        read_only = True
        border_title = "working"
        focused = False

        def focus(self) -> None:
            self.focused = True

    app = CodeRookTuiApp("127.0.0.1", 9999)
    prompt = _FakePrompt()
    states: list[str] = []
    app._busy = True  # type: ignore[attr-defined]
    app._active_run_id = "run-1"  # type: ignore[attr-defined]
    app._cancel_requested = True  # type: ignore[attr-defined]
    app._prompt = lambda: prompt  # type: ignore[method-assign]
    app._update_header = lambda state: states.append(state)  # type: ignore[method-assign]

    app._handle_event({
        "type": "session.interrupted",
        "session_id": "sess-1",
        "last_run_id": "run-1",
        "reason": "cancelled",
        "ts": "t",
    })

    assert not app._busy  # type: ignore[attr-defined]
    assert app._active_run_id is None  # type: ignore[attr-defined]
    assert not app._cancel_requested  # type: ignore[attr-defined]
    assert not prompt.disabled
    assert not prompt.read_only
    assert prompt.focused
    assert "cancelled" in prompt.border_title
    assert states == ["interrupted"]


# 功能：验证未知事件类型不抛异常也不追加任何 widget
# 设计：发送 type 为 unknown 的事件，断言 appended 为空
def test_unknown_event_silently_ignored() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "some.unknown.type", "run_id": "r", "ts": "t"})
    assert appended == []

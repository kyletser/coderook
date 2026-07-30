from __future__ import annotations

import pytest
from rich.markup import render
from textual.app import App, ComposeResult
from textual.widget import Widget
from textual.widgets import Input, Markdown

from code_rook.core.llm.provider_presets import PROVIDER_PRESETS
from code_rook.tui import app as tui_app_module
from code_rook.tui.app import (
    ChatTextArea,
    CodeRookTuiApp,
    ConfigApiKeyPrompt,
    ConfigSwitch,
    LLMStreamBlock,
    ModelPicker,
    PermissionBlock,
    PermissionSelect,
    ProviderPicker,
    SessionPicker,
    SlashCompleteWidget,
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


# 功能：验证长模型列表只渲染光标附近窗口并提示剩余数量
# 设计：直接移动内部光标后检查纯文本，确保真实 API 返回大量模型时选中项始终可见
def test_model_picker_keeps_cursor_visible_in_long_list() -> None:
    models = [f"model-{index}" for index in range(20)]
    picker = ModelPicker(models, "model-0")
    picker._cursor = 15

    plain = render(picker._render_ui()).plain

    assert "model-15" in plain
    assert "more" in plain
    assert "model-0" not in plain


# 功能：验证 /config Provider 选择器显示四种内置接入方式并支持键盘选择
# 设计：在最小 Textual App 中选择第二项，覆盖中文名称渲染和 Selected 消息
async def test_provider_picker_shows_four_builtin_options() -> None:
    class PickerHarness(App[None]):
        # 初始化测试宿主并收集 Provider 结果
        def __init__(self) -> None:
            super().__init__()
            self.selected: list[str] = []

        # 挂载 Provider 选择器
        def compose(self) -> ComposeResult:
            yield ProviderPicker(PROVIDER_PRESETS, "deepseek")

        # 接收 Provider 选择消息
        def on_provider_picker_selected(self, message: ProviderPicker.Selected) -> None:
            self.selected.append(message.provider)

    app = PickerHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        picker = app.query_one(ProviderPicker)
        plain = render(picker._render_ui()).plain
        assert "DeepSeek API" in plain
        assert "OpenAI" in plain
        assert "Anthropic" in plain
        assert "硅基流动" in plain

        await pilot.press("down", "enter")
        await pilot.pause()
        assert app.selected == ["openai"]


# 功能：验证 API Key 在当前页面以密码输入方式提交
# 设计：设置 Input 值后触发 Enter，断言 password 属性和消息值且渲染文本不含密钥
async def test_config_api_key_prompt_masks_and_submits_key() -> None:
    provider = PROVIDER_PRESETS[0]

    class PromptHarness(App[None]):
        # 初始化测试宿主并收集密钥消息
        def __init__(self) -> None:
            super().__init__()
            self.keys: list[str] = []

        # 挂载密码输入面板
        def compose(self) -> ComposeResult:
            yield ConfigApiKeyPrompt(provider)

        # 接收 API Key 提交消息
        def on_config_api_key_prompt_submitted(
            self,
            message: ConfigApiKeyPrompt.Submitted,
        ) -> None:
            self.keys.append(message.api_key)

    app = PromptHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        key_input = app.query_one("#config-api-key", Input)
        assert key_input.password is True
        key_input.value = "secret-test-key"

        await pilot.press("enter")
        await pilot.pause()

        assert app.keys == ["secret-test-key"]
        assert "secret-test-key" not in str(app.screen.render())


# 功能：验证跨 TUI 重启传递配置时 dataclass 表示不会暴露 API Key
# 设计：直接检查 ConfigSwitch 的 repr，覆盖异常日志或调试输出中的密钥泄漏风险
def test_config_switch_repr_hides_api_key() -> None:
    action = ConfigSwitch(
        provider="openai",
        api_key="secret-test-key",
        model="gpt-5.6-terra",
        models=("gpt-5.6-terra",),
        session_id="session-1",
    )

    assert "secret-test-key" not in repr(action)


# 功能：验证 /config 在当前页面完成 Provider、Key 探测和模型选择全流程
# 设计：禁用真实 socket 并替换 Models API，使用真实按键驱动三个界面后检查 ConfigSwitch
async def test_inline_config_flow_returns_verified_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_discover(provider: object, api_key: str) -> list[str]:
        assert api_key == "verified-key"
        return ["gpt-5.6-sol", "gpt-5.6-terra"]

    monkeypatch.setattr(tui_app_module, "discover_models", fake_discover)

    class ConfigHarness(CodeRookTuiApp):
        # 挂载测试界面但不启动真实 socket worker
        def on_mount(self) -> None:
            self._slash_items = self._build_slash_items()
            self._session_id = "session-inline"
            self.query_one("#prompt", ChatTextArea).focus()

    app = ConfigHarness(
        "127.0.0.1",
        9999,
        provider="openai",
        model="gpt-5.6-sol",
    )
    async with app.run_test(size=(110, 26)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "/config"
        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        await pilot.pause()
        assert app.query_one(ProviderPicker)

        await pilot.press("enter")
        await pilot.pause()
        key_input = app.query_one("#config-api-key", Input)
        key_input.value = "verified-key"
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one(ModelPicker)

        await pilot.press("down", "enter")
        await pilot.pause()

    assert app.return_value == ConfigSwitch(
        provider="openai",
        api_key="verified-key",
        model="gpt-5.6-terra",
        models=("gpt-5.6-sol", "gpt-5.6-terra"),
        session_id="session-inline",
    )


# 功能：验证完整输入 /model 后第一次 Enter 直接执行而不是只确认补全
# 设计：挂载真实输入框和补全弹窗，直接断言一次 Enter 发布 Submitted 而非 Selected
async def test_exact_slash_command_runs_on_first_enter() -> None:
    class SlashHarness(App[None]):
        # 初始化消息收集器
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[str] = []
            self.selected: list[str] = []

        # 挂载命令补全弹窗和聊天输入框
        def compose(self) -> ComposeResult:
            yield SlashCompleteWidget([("model", "switch model")])
            yield ChatTextArea(id="prompt", show_line_numbers=False)

        # 设置完整命令并把键盘焦点交给输入框
        def on_mount(self) -> None:
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/model"
            prompt.focus()

        # 收集输入框提交消息
        def on_chat_text_area_submitted(self, message: ChatTextArea.Submitted) -> None:
            self.submitted.append(message.value)

        # 收集命令补全选择消息
        def on_slash_complete_widget_selected(
            self,
            message: SlashCompleteWidget.Selected,
        ) -> None:
            self.selected.append(message.skill_name)

    app = SlashHarness()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert app.submitted == ["/model"]
        assert app.selected == []


# 功能：验证未完整输入的斜杠命令仍由第一次 Enter 完成补全
# 设计：挂载真实输入框和补全弹窗，直接断言一次 Enter 发布 Selected 而非 Submitted
async def test_partial_slash_command_enter_only_completes() -> None:
    class SlashHarness(App[None]):
        # 初始化消息收集器
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[str] = []
            self.selected: list[str] = []

        # 挂载命令补全弹窗和聊天输入框
        def compose(self) -> ComposeResult:
            yield SlashCompleteWidget([("model", "switch model")])
            yield ChatTextArea(id="prompt", show_line_numbers=False)

        # 设置部分命令、筛选候选并把键盘焦点交给输入框
        def on_mount(self) -> None:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query("mo")
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/mo"
            prompt.focus()

        # 收集输入框提交消息
        def on_chat_text_area_submitted(self, message: ChatTextArea.Submitted) -> None:
            self.submitted.append(message.value)

        # 收集命令补全选择消息
        def on_slash_complete_widget_selected(
            self,
            message: SlashCompleteWidget.Selected,
        ) -> None:
            self.selected.append(message.skill_name)

    app = SlashHarness()
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        await pilot.press("enter")
        await pilot.pause()

        assert app.submitted == []
        assert app.selected == ["model"]


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


# 功能：验证 LLMStreamBlock 结束时切换为支持屏幕选择的 Textual Markdown
# 设计：在真实 App 中完成流式块并重组子组件，断言最终挂载 Markdown 而非不可选 Rich renderable
async def test_llm_block_finalize_renders_selectable_markdown() -> None:
    block = LLMStreamBlock()
    block.append_token("## Title\n\n- one\n\n```python\nprint('hi')\n```")

    class MarkdownHarness(App[None]):
        # 挂载已包含流式文本的回复块
        def compose(self) -> ComposeResult:
            yield block

    app = MarkdownHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        block.finalize_markdown()
        await pilot.pause()

        markdown = block.query_one(Markdown)
        assert "## Title" in markdown.source


# 功能：验证 Ctrl+C 在存在屏幕选择时复制文本且不取消任务
# 设计：替换屏幕选择读取并记录取消调用，直接执行绑定动作检查复制优先级
async def test_ctrl_c_copies_selection_before_cancel() -> None:
    class CopyHarness(CodeRookTuiApp):
        # 初始化取消计数并跳过真实 socket 连接
        def __init__(self) -> None:
            super().__init__("127.0.0.1", 9999)
            self.cancel_count = 0

        # 聚焦输入框但不连接 Core
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

        # 记录取消调用，验证复制分支不会触发
        async def action_cancel_run(self) -> None:
            self.cancel_count += 1

    app = CopyHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.screen.get_selected_text = lambda: "selected output"  # type: ignore[method-assign]

        await app.action_copy_or_cancel()

        assert app.clipboard == "selected output"
        assert app.cancel_count == 0


# 功能：验证 Ctrl+C 没有屏幕选择时回退到原有取消任务动作
# 设计：替换屏幕选择为空并记录取消次数，确认复制改造不破坏运行中断语义
async def test_ctrl_c_without_selection_cancels() -> None:
    class CopyHarness(CodeRookTuiApp):
        # 初始化取消计数并跳过真实 socket 连接
        def __init__(self) -> None:
            super().__init__("127.0.0.1", 9999)
            self.cancel_count = 0

        # 聚焦输入框但不连接 Core
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

        # 记录取消调用，验证无选择分支会触发
        async def action_cancel_run(self) -> None:
            self.cancel_count += 1

    app = CopyHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.screen.get_selected_text = lambda: None  # type: ignore[method-assign]

        await app.action_copy_or_cancel()

        assert app.clipboard == ""
        assert app.cancel_count == 1


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

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import pytest
from rich.markup import render
from textual.app import App, ComposeResult
from textual.containers import ScrollableContainer
from textual.widget import Widget
from textual.widgets import Input, Markdown, Static

from code_rook.core.authority import RuntimeMode
from code_rook.core.llm.doctor import ProviderDoctorResult
from code_rook.core.llm.provider_presets import PROVIDER_PRESETS
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import get_route_preset
from code_rook.tui import app as tui_app_module
from code_rook.tui.app import (
    ChatTextArea,
    CheckpointPicker,
    CodeRookTuiApp,
    ConfigApiKeyPrompt,
    ConfigSwitch,
    LLMStreamBlock,
    ModelPicker,
    PermissionBlock,
    PermissionModePicker,
    PermissionSelect,
    PlanReview,
    ProviderPicker,
    SessionPicker,
    SlashCompleteWidget,
    ToolCallBlock,
    ToolStepGroup,
    UserQuestionSelect,
    _param_summary,
    _preview,
    _tool_action_text,
    _tool_target,
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


# 功能：统一 agent action 在 TUI 中显示自然的 Worker 动作和目标
# 设计：直接验证 start 与 wait 文案，覆盖 action-family 不再回退为原始参数摘要
def test_agent_action_uses_worker_labels() -> None:
    start = {"action": "start", "description": "inspect module"}
    wait = {"action": "wait", "worker_id": "worker-1"}

    assert _tool_target("agent", start) == "inspect module"
    assert _tool_action_text("agent", start, finished=False) == (
        "正在启动 Worker inspect module"
    )
    assert _tool_action_text("agent", wait, finished=True) == (
        "已等待 Worker worker-1"
    )


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


# 功能：验证审批完成后移除临时权限块，避免与工具动作重复显示
# 设计：在真实 TUI 中注入权限请求并允许一次，检查选择器和摘要块均从时间线消失
async def test_permission_decision_leaves_no_duplicate_summary() -> None:
    class PermissionTimelineHarness(CodeRookTuiApp):
        # 跳过真实 Core 连接并聚焦主输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = PermissionTimelineHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._handle_event(
            {
                "type": "permission.requested",
                "run_id": "run-1",
                "tool_use_id": "tool-1",
                "tool_name": "bash",
                "param_preview": "command='git status'",
                "params": {"command": "git status"},
            }
        )
        await pilot.pause()
        select = app.query_one(PermissionSelect)

        await app.on_permission_select_decided(
            PermissionSelect.Decided(select, "tool-1", "allow_once")
        )
        await pilot.pause()

        assert len(app.query(PermissionSelect)) == 0
        assert len(app.query(PermissionBlock)) == 0


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


# 功能：验证计划审阅面板可用键盘选择继续规划并用 Esc 取消
# 设计：在最小 Textual App 中发送真实按键，覆盖默认批准、移动选择和取消消息
async def test_plan_review_keyboard_decisions() -> None:
    class ReviewHarness(App[None]):
        # 初始化决定收集器
        def __init__(self) -> None:
            super().__init__()
            self.decisions: list[str] = []

        # 挂载计划审阅面板
        def compose(self) -> ComposeResult:
            yield PlanReview("run-plan")

        # 收集计划审阅决定
        def on_plan_review_decided(self, message: PlanReview.Decided) -> None:
            self.decisions.append(message.decision)

    app = ReviewHarness()
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        review = app.query_one(PlanReview)
        assert review.has_focus
        assert "批准并实施" in render(review._render_ui()).plain

        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert app.decisions == ["revise", "cancel"]


# 功能：验证权限模式选择器可用键盘选择自动接受修改并用 Esc 关闭
# 设计：在最小 Textual App 中发送真实按键，覆盖当前态标识、选择消息和关闭消息
async def test_permission_mode_picker_keyboard_flow() -> None:
    class PermissionHarness(App[None]):
        # 初始化权限模式选择结果
        def __init__(self) -> None:
            super().__init__()
            self.selected: list[str] = []
            self.dismissed = 0

        # 挂载权限模式选择器
        def compose(self) -> ComposeResult:
            yield PermissionModePicker("ask")

        # 收集权限模式选择消息
        def on_permission_mode_picker_selected(
            self,
            message: PermissionModePicker.Selected,
        ) -> None:
            self.selected.append(message.preset)

        # 收集权限模式关闭消息
        def on_permission_mode_picker_dismissed(
            self,
            _message: PermissionModePicker.Dismissed,
        ) -> None:
            self.dismissed += 1

    app = PermissionHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        picker = app.query_one(PermissionModePicker)
        assert "询问后修改" in render(picker._render_ui()).plain
        assert "全自动执行" in render(picker._render_ui()).plain
        assert "current" in render(picker._render_ui()).plain

        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert app.selected == ["accept_edits"]
        assert app.dismissed == 1


# 功能：验证结构化问题选择器支持单选、多选和自由回答入口
# 设计：在真实 Textual 消息泵中发送键盘操作，检查答案顺序和 Esc 自由回答语义
async def test_user_question_select_keyboard_flow() -> None:
    class QuestionHarness(App[None]):
        # 初始化问题回答收集器
        def __init__(self) -> None:
            super().__init__()
            self.answers: list[str | None] = []

        # 挂载多选问题
        def compose(self) -> ComposeResult:
            yield UserQuestionSelect(
                "question-1",
                "选择测试范围",
                "测试",
                ["单元测试", "集成测试"],
                True,
            )

        # 收集结构化问题答案
        def on_user_question_select_answered(
            self,
            message: UserQuestionSelect.Answered,
        ) -> None:
            self.answers.append(message.answer)

    app = QuestionHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        select = app.query_one(UserQuestionSelect)
        assert "选择测试范围" in render(select._render_ui()).plain

        await pilot.press("space", "down", "enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert app.answers == ["单元测试, 集成测试", None]


# 功能：验证 checkpoint 选择器通过键盘选择指定恢复点并支持取消
# 设计：挂载两个真实形状的 checkpoint，移动后确认 ID，再用 Esc 验证无修改关闭路径
async def test_checkpoint_picker_keyboard_flow() -> None:
    checkpoints = [
        {
            "checkpoint_id": "20260731T010203-aaaaaaaa",
            "label": "edit auth",
            "created_at": "2026-07-31T01:02:03Z",
            "status": "ready",
            "paths": ["src/auth.py"],
        },
        {
            "checkpoint_id": "20260731T010204-bbbbbbbb",
            "label": "edit tests",
            "created_at": "2026-07-31T01:02:04Z",
            "status": "ready",
            "paths": ["tests/test_auth.py"],
        },
    ]

    class CheckpointHarness(App[None]):
        # 初始化 checkpoint 选择结果
        def __init__(self) -> None:
            super().__init__()
            self.selected: list[str] = []
            self.dismissed = 0

        # 挂载 checkpoint 选择器
        def compose(self) -> ComposeResult:
            yield CheckpointPicker(checkpoints)

        # 收集 checkpoint 选择消息
        def on_checkpoint_picker_selected(
            self,
            message: CheckpointPicker.Selected,
        ) -> None:
            self.selected.append(message.checkpoint_id)

        # 收集 checkpoint 取消消息
        def on_checkpoint_picker_dismissed(
            self,
            _message: CheckpointPicker.Dismissed,
        ) -> None:
            self.dismissed += 1

    app = CheckpointHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        await pilot.press("down", "enter")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert app.selected == ["20260731T010204-bbbbbbbb"]
        assert app.dismissed == 1


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


# 功能：验证 /config 在当前页面完成 Provider、Key 探测、模型选择和 route 持久化
# 设计：注入临时 RouteStore 与凭据 stub，使用真实按键驱动三个界面并检查无需退出 TUI
async def test_inline_config_flow_returns_verified_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
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

    class _Credentials:
        # 返回稳定凭据引用并避免测试访问操作系统 keyring
        def save(self, route_id: str, api_key: str) -> str:
            assert api_key == "verified-key"
            return f"file:{route_id}"

    routes = RouteStore(tmp_path / "routes.json")
    app = ConfigHarness(
        "127.0.0.1",
        9999,
        provider="openai",
        model="gpt-5.6-sol",
        route_store=routes,
        credential_store=_Credentials(),  # type: ignore[arg-type]
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

    assert app.return_value is None
    assert routes.active() is not None
    assert routes.active().id == "openai"  # type: ignore[union-attr]
    assert routes.active().model == "gpt-5.6-terra"  # type: ignore[union-attr]
    assert app._route == "openai"
    assert app._model == "gpt-5.6-terra"


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
    assert items["provider"] == "show or switch the active provider route"
    assert items["doctor"] == "diagnose the active provider route"
    assert items["copy"] == "copy the latest assistant reply"
    assert items["plan"] == "analyze read-only and review a plan before implementation"
    assert items["permissions"] == "review or change the permission mode"
    assert items["tasks"] == "show tasks from the latest run"
    assert items["workers"] == "show all durable workers and Fleet workers"
    assert items["workflow"] == "list, start, or inspect durable workflows"
    assert items["diff"] == "show current workspace changes"
    assert items["rewind"] == "restore files from a safe checkpoint"
    assert items["context"] == "show context size and usage"
    assert items["skills"] == "list, show, install, remove, or audit skills"


# 功能：验证 TUI /skills install 先展示 preview，追加 --yes 后才写入项目目录
# 设计：直接调用本地命令 handler 并收集 Static，避免 socket 干扰文件确认语义
def test_tui_skills_install_requires_explicit_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: tui-skill\ndescription: TUI test\n---\nDo work\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_skills_command(f'/skills install "{source}" --trust')

    target = tmp_path / ".coderook" / "skills" / "tui-skill"
    assert not target.exists()
    assert "安装预览" in "\n".join(str(widget.render()) for widget in appended)

    app._handle_skills_command(f'/skills install "{source}" --trust --yes')

    assert target.is_dir()


# 功能：验证 TUI 的 Provider 与模型切换直接更新同一用户级 RouteStore
# 设计：注入两个临时 route 后调用界面动作，断言活动项和模型无需重启 Core 即刻变化
def test_tui_route_and_model_switch_update_route_store(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    routes.add(get_route_preset("anthropic"), activate=True)
    routes.add(get_route_preset("openai"))
    app = CodeRookTuiApp("127.0.0.1", 9999, route_store=routes)
    appended: list[Widget] = []
    states: list[str] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._update_header = lambda state: states.append(state)  # type: ignore[method-assign]

    app._select_provider_route("openai")
    app._select_route_model("gpt-route-test")

    active = routes.active()
    assert active is not None
    assert active.id == "openai"
    assert active.model == "gpt-route-test"
    assert app._route == "openai"
    assert app._model == "gpt-route-test"
    assert states == ["ready", "ready"]
    assert len(appended) == 2


# 功能：验证 TUI doctor 显示分类和凭据来源，但不显示任何 API key 正文
# 设计：注入固定诊断器与凭据 stub，调用真实展示方法并检查渲染文本和输入恢复
async def test_tui_doctor_renders_redacted_result(tmp_path: Path) -> None:
    class _Credentials:
        # 返回包含测试密钥的解析结果，验证展示层不会读取 value
        def resolve(self, _credential_ref: str) -> object:
            from code_rook.core.llm.credentials import CredentialResolution

            return CredentialResolution(value="top-secret", source="file")

    class _Doctor:
        # 返回固定成功分类，避免测试访问真实网络
        async def check(self, _route: object, _credential: object) -> ProviderDoctorResult:
            return ProviderDoctorResult(
                status="ok",
                category="ok",
                route_id="openai",
                message="route is ready",
                credential_source="file",
                http_status=200,
            )

    routes = RouteStore(tmp_path / "routes.json")
    routes.add(get_route_preset("openai"), activate=True)
    app = CodeRookTuiApp(
        "127.0.0.1",
        9999,
        route_store=routes,
        credential_store=_Credentials(),  # type: ignore[arg-type]
        provider_doctor=_Doctor(),  # type: ignore[arg-type]
    )
    appended: list[Static] = []
    restored: list[bool] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: restored.append(True)  # type: ignore[method-assign]

    await app._show_provider_doctor()

    rendered = "\n".join(str(widget.render()) for widget in appended)
    assert "route is ready" in rendered
    assert "credential=file" in rendered
    assert "top-secret" not in rendered
    assert restored == [True]


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
    assert _param_summary("task_create", {"subject": "Inspect events"}) == (
        "subject='Inspect events'"
    )


# 功能：验证常见工具使用自然动作文案且摘要没有额外左缩进
# 设计：渲染运行中与完成态摘要，覆盖三种高频文案并检查图标从控件左边界开始
def test_tool_summary_uses_natural_action_text() -> None:
    bash = ToolCallBlock("bash", {"command": "git status"})
    read = ToolCallBlock("read_file", {"path": "README.md"})
    family_read = ToolCallBlock("File", {"action": "read", "path": "README.md"})
    search = ToolCallBlock("grep", {"pattern": "TODO", "path": "src"})

    bash_summary = render(bash._summary()).plain  # type: ignore[attr-defined]
    assert bash_summary.startswith("◌")
    assert "正在执行命令 git status" in bash_summary
    read.set_result("content", 2)
    family_read.set_result("content", 2)
    search.set_result("match", 3)
    read_summary = render(read._summary()).plain  # type: ignore[attr-defined]
    assert read_summary.startswith("✓")
    assert "已读取 README.md" in read_summary
    assert "已读取 README.md" in render(family_read._summary()).plain  # type: ignore[attr-defined]
    assert "已搜索 TODO in src" in render(search._summary()).plain  # type: ignore[attr-defined]


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


# 功能：验证独立 reasoning 事件才会生成已完成的深度思考块
# 设计：先发送英文 reasoning 再发送最终 token，断言两者进入不同组件且不会被错误拼接
def test_reasoning_event_is_separate_from_final_text() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_event({
        "type": "llm.reasoning",
        "content": "I should inspect the project first.",
        "run_id": "r",
        "ts": "t",
    })
    app._handle_event({
        "type": "llm.token",
        "token": "最终答案",
        "run_id": "r",
        "ts": "t",
    })

    assert len(appended) == 2
    reasoning = appended[0]
    answer = appended[1]
    assert isinstance(reasoning, LLMStreamBlock)
    assert reasoning._finalized  # type: ignore[attr-defined]
    assert reasoning._text == "I should inspect the project first."  # type: ignore[attr-defined]
    assert isinstance(answer, LLMStreamBlock)
    assert answer._text == "最终答案"  # type: ignore[attr-defined]


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


# 功能：验证思考内容显示时间线标题，而最终回答隐藏思考标题
# 设计：在真实挂载状态下先检查思考头，再切换 answer class 并检查标题的计算显示状态
async def test_llm_block_switches_from_thinking_timeline_to_answer() -> None:
    block = LLMStreamBlock()
    block.append_token("正在分析项目。")

    class ThinkingHarness(App[None]):
        # 挂载流式块以读取 Textual 计算后的 CSS 状态
        def compose(self) -> ComposeResult:
            yield block

    app = ThinkingHarness()
    async with app.run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        header = block.query_one(".message-kind", Static)
        assert header.display
        assert "思考中" in render(str(header.content)).plain

        block.set_kind("answer")
        block.finalize_markdown()
        await pilot.pause()

        assert "answer" in block.classes
        assert not block.query_one(".message-kind", Static).display


# 功能：验证 agent.decision 完成当前思考块并保留实际动作意图状态
# 设计：先发送 token 再发送 inspect 决策，断言同一消息块完成且未误切换为最终回答样式
def test_agent_decision_labels_visible_progress_as_intent() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_event({"type": "llm.token", "token": "我先检查配置。", "run_id": "r"})
    app._handle_event({
        "type": "agent.decision",
        "run_id": "r",
        "step": 1,
        "intent": "inspect",
        "summary": "我先检查配置。",
        "tool_names": ["read_file"],
        "has_visible_text": True,
    })

    assert len(appended) == 1
    block = appended[0]
    assert isinstance(block, LLMStreamBlock)
    assert block._kind == "inspect"  # type: ignore[attr-defined]
    assert block._finalized  # type: ignore[attr-defined]


# 功能：验证没有模型进度文本时不额外显示机械化 intent 摘要
# 设计：直接发送无可见文本的执行决策，断言工具行可独立表达动作而时间线不增加噪声
def test_agent_decision_without_text_stays_silent() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_event({
        "type": "agent.decision",
        "run_id": "r",
        "step": 1,
        "intent": "execute",
        "summary": "Using bash",
        "tool_names": ["bash"],
        "has_visible_text": False,
    })

    assert appended == []


# 功能：验证 Ctrl+C 在存在屏幕选择时复制文本且不取消任务
# 设计：替换屏幕选择读取并记录取消调用，直接执行绑定动作检查复制优先级
async def test_ctrl_c_copies_selection_before_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(tui_app_module, "copy_to_windows_clipboard", lambda _text: True)
    app = CopyHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.screen.get_selected_text = lambda: "selected output"  # type: ignore[method-assign]

        await app.action_copy_or_cancel()

        assert app.clipboard == "selected output"
        assert app.cancel_count == 0


# 功能：验证 Ctrl+C 没有屏幕选择时回退到原有取消任务动作
# 设计：替换屏幕选择为空并记录取消次数，确认复制改造不破坏运行中断语义
async def test_ctrl_c_without_selection_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr(tui_app_module, "copy_to_windows_clipboard", lambda _text: True)
    app = CopyHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.screen.get_selected_text = lambda: None  # type: ignore[method-assign]

        await app.action_copy_or_cancel()

        assert app.clipboard == ""
        assert app.cancel_count == 1


# 功能：验证 Ctrl+Shift+C 在没有拖选文本时复制最近一条完整回复
# 设计：挂载真实 TUI、替换系统剪贴板后端并设置最后回复，覆盖选择失败时的可靠降级路径
async def test_copy_shortcut_falls_back_to_last_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        tui_app_module,
        "copy_to_windows_clipboard",
        lambda text: copied.append(text) is None,
    )

    class CopyHarness(CodeRookTuiApp):
        # 跳过真实 Core 连接并准备可复制回复
        def on_mount(self) -> None:
            self._last_assistant_text = "完整回复内容"
            self.query_one("#prompt", ChatTextArea).focus()

    app = CopyHarness("127.0.0.1", 9999)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        app.screen.get_selected_text = lambda: None  # type: ignore[method-assign]

        app.action_copy_selection()

        assert app.clipboard == "完整回复内容"
        assert copied == ["完整回复内容"]


# 功能：验证 /copy 第一次提交即可复制上一条回复且不发送给 agent
# 设计：直接触发真实输入提交事件并替换剪贴板后端，断言输入被清空且没有进入 busy 状态
async def test_copy_command_copies_last_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        tui_app_module,
        "copy_to_windows_clipboard",
        lambda text: copied.append(text) is None,
    )

    class CopyHarness(CodeRookTuiApp):
        # 跳过真实 socket 并初始化聊天输入
        def on_mount(self) -> None:
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/copy"
            prompt.focus()
            self._last_assistant_text = "last answer"

    app = CopyHarness("127.0.0.1", 9999)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))

        assert prompt.text == ""
        assert not app._busy
        assert copied == ["last answer"]


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
    assert app._last_assistant_text == "A"


# 功能：验证 run.started 只更新活动运行状态而不重复显示用户目标和内部 ID
# 设计：截获追加控件并发送真实形状事件，断言状态被记录且时间线保持安静
def test_run_started_updates_state_without_timeline_noise() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.started", "run_id": "run-abc", "goal": "do the thing", "ts": "t"
    })

    assert app._active_run_id == "run-abc"
    assert appended == []


# 功能：验证 step.started 和 llm.usage 不再向主时间线追加内部元数据
# 设计：连续注入步骤与用量事件，断言仅更新上下文占用状态且没有产生可见控件
def test_step_and_usage_events_stay_out_of_timeline() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_event({"type": "step.started", "run_id": "run-1", "step": 2})
    app._handle_event(
        {
            "type": "llm.usage",
            "run_id": "run-1",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "context_pct": 0.25,
        }
    )

    assert appended == []
    assert app._last_context_pct == 0.25


# 功能：验证 retry 与 stuck 可靠性事件以轻量摘要进入时间线
# 设计：直接注入两种结构化事件，断言重试类别、工具名和重复次数均可观察
def test_retry_and_stuck_events_show_bounded_timeline_summaries() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._handle_event(
        {"type": "llm.retry", "kind": "no_content", "attempt": 1}
    )
    app._handle_event(
        {
            "type": "agent.stuck",
            "tool_name": "File",
            "repeat_count": 3,
        }
    )

    rendered = [str(widget.content) for widget in appended]  # type: ignore[attr-defined]
    assert "no_content #1" in rendered[0]
    assert "File" in rendered[1]
    assert "3 identical results" in rendered[1]


# 功能：验证 run.finished success 只结束运行状态而不追加完成提示
# 设计：截获可见控件并发送成功事件，断言最终回答之后不再出现重复的 completed 状态行
def test_run_finished_success_stays_out_of_timeline() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._active_run_id = "r"
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "success", "steps": 3, "ts": "t"
    })

    assert app._active_run_id is None
    assert appended == []


# 功能：验证 run.finished failed 追加轻量失败摘要
# 设计：与成功态对称检查失败图标、步骤数和原因，保留必要诊断而不显示内部运行头
def test_run_finished_failed_shows_red() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({
        "type": "run.finished", "run_id": "r", "status": "failed",
        "steps": 1, "reason": "llm_error", "ts": "t"
    })

    rendered = appended[0].content
    assert "×" in rendered
    assert "Failed after 1 step" in rendered
    assert "llm_error" in rendered


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
    group = appended[0]
    assert isinstance(group, ToolStepGroup)
    block = group._blocks[0]  # type: ignore[attr-defined]
    assert isinstance(block, ToolCallBlock)
    assert block._finished  # type: ignore[attr-defined]
    assert block._output == "hi"  # type: ignore[attr-defined]
    summary = render(block._summary()).plain  # type: ignore[attr-defined]
    assert "Running" not in summary
    assert "已执行命令 echo hi" in summary


# 功能：验证快速成功工具默认紧凑且可完整展开输入、结果和状态
# 设计：先传入多行输出再挂载控件，复现零毫秒竞态并核对展开面板内容与滚动容器
async def test_tool_block_fast_completion_expands_full_result() -> None:
    block = ToolCallBlock("bash", {"command": "test"})
    block.set_result("one\ntwo\nthree\nfour", 12)

    class ToolHarness(App[None]):
        # 挂载待完成的工具调用块
        def compose(self) -> ComposeResult:
            yield block

    app = ToolHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        detail = block.query_one(".detail", ScrollableContainer)
        detail_content = block.query_one(".detail-content", Static)

        assert "finished" in block.classes
        assert "expanded" not in block.classes
        assert not detail.display
        assert str(detail_content.content) == ""

        block.set_expanded(True)
        await pilot.pause()

        assert "expanded" in block.classes
        assert detail.display
        rendered_detail = str(detail_content.content)
        assert "bash\ntest" in rendered_detail
        assert "Response\none\ntwo\nthree\nfour" in rendered_detail
        assert "✓ 成功 · 12 ms" in rendered_detail


# 功能：验证失败工具仍可展开查看错误信息
# 设计：挂载已失败工具并显式展开，断言错误保留而成功态隐藏规则不影响故障诊断
async def test_failed_tool_block_expands_error_details() -> None:
    block = ToolCallBlock("bash", {"command": "bad"})
    block.set_result("command failed", 7, is_error=True)

    class FailedToolHarness(App[None]):
        # 挂载已失败工具调用块
        def compose(self) -> ComposeResult:
            yield block

    app = FailedToolHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        block.set_expanded(True)
        await pilot.pause()

        assert "expanded" in block.classes
        assert "command failed" in str(block.query_one(".detail-content", Static).content)


# 功能：验证同一 step 的多个工具被合并到一个可折叠步骤组
# 设计：先挂载一个工具再动态追加第二个，检查标题计数、子项数量和标题点击折叠行为
async def test_tool_step_group_collects_and_collapses_tools() -> None:
    first = ToolCallBlock("read_file", {"path": "README.md"})
    second = ToolCallBlock("grep", {"pattern": "TODO", "path": "src"})
    group = ToolStepGroup(1)
    group.add_tool(first)

    class StepHarness(App[None]):
        # 挂载步骤组以验证动态追加和样式状态
        def compose(self) -> ComposeResult:
            yield group

    app = StepHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        group.add_tool(second)
        await pilot.pause()
        header = group.query_one(".step-header", Static)

        assert "读取了文件 · 搜索了内容" in render(str(header.content)).plain
        assert len(group.query(ToolCallBlock)) == 2

        event = type("Click", (), {"widget": header})()
        group.on_click(event)  # type: ignore[arg-type]
        assert "collapsed" in group.classes


# 功能：验证 note_save 成功完成后使用自然动作文案
# 设计：直接操作 ToolCallBlock，覆盖 note_save 的专用名称映射而非暴露内部标识
def test_note_save_tool_block_uses_natural_action() -> None:
    block = ToolCallBlock("note_save", {"content": "Python 3.12"})
    block.set_result("saved", 3)
    assert "已保存笔记" in render(block._summary()).plain  # type: ignore[attr-defined]


# 功能：验证提交新消息后保留输入框可用，使用户能在 run 中继续输入纠偏
# 设计：用轻量 fake 输入框启动真实消息路径，检查 busy、提示文字和用户消息渲染
async def test_input_submit_appends_user_turn_and_keeps_prompt_for_steering() -> None:
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
    assert not area.disabled
    assert area.border_title == "执行中 · Enter 补充 · Ctrl+C 取消"
    assert area.text == ""
    assert area.border_title == "执行中 · Enter 补充 · Ctrl+C 取消"
    assert appended[0].content == "hello"


# 功能：验证 /plan 描述单次提交进入只读 run，计划完成后批准才发送 Act run
# 设计：使用真实 TUI 消息泵和计划审阅控件、fake IPC，核对两次 session.send_message 的 mode 顺序
async def test_plan_command_requires_review_before_act() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        # 记录 TUI 发出的 IPC 命令
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            if method == "session.set_authority":
                return {
                    "snapshot": {
                        "mode": params.get("mode", "act"),
                        "profile": params.get("profile", "ask"),
                    }
                }
            return {"run_id": "run-plan"}

    class PlanHarness(CodeRookTuiApp):
        # 跳过 socket worker并准备输入框
        def on_mount(self) -> None:
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/plan inspect authentication"
            prompt.focus()

    app = PlanHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)
        scheduled: list[asyncio.Task[None]] = []

        # 用普通 asyncio task 执行 worker coroutine，避免等待 Textual 全局 worker 集合
        def schedule(
            coroutine: Coroutine[Any, Any, None],
            **_kwargs: object,
        ) -> asyncio.Task[None]:
            task = asyncio.create_task(coroutine)
            scheduled.append(task)
            return task

        app.run_worker = schedule  # type: ignore[method-assign]

        app._client = _FakeClient()  # type: ignore[assignment]
        app._session_id = "sess-plan"
        prompt.text = "/plan inspect authentication"
        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        assert app._busy
        assert scheduled
        await asyncio.gather(*scheduled)
        scheduled.clear()

        assert calls[0] == (
            "session.send_message",
            {
                "session_id": "sess-plan",
                "content": "inspect authentication",
                "runtime_mode": "plan",
            },
        )
        app._handle_event(
            {
                "type": "plan.ready",
                "session_id": "sess-plan",
                "run_id": "run-plan",
                "request": "inspect authentication",
                "plan": "1. Inspect\n2. Edit\n3. Test",
                "ts": "t",
            }
        )
        app._handle_event(
            {
                "type": "session.waiting_for_input",
                "session_id": "sess-plan",
                "last_run_id": "run-plan",
                "ts": "t",
            }
        )
        await pilot.pause()

        assert prompt.disabled
        assert app.query_one(PlanReview).has_focus
        assert len(calls) == 1

        await pilot.press("enter")
        await pilot.pause()
        await asyncio.gather(*scheduled)

        assert calls[1][0] == "session.set_authority"
        assert calls[2][0] == "session.send_message"
        assert calls[2][1]["runtime_mode"] == "act"
        assert str(calls[2][1]["content"]).startswith("Implement the approved plan")
        assert "Original user request:\ninspect authentication" in str(
            calls[2][1]["content"]
        )


# 功能：验证单独输入 /plan 会直接进入下一条消息的计划模式而不发送空 run
# 设计：在真实 TUI 中提交完整命令一次，检查输入状态和 mode，固定无需第二次确认补全
async def test_plan_command_enters_mode_on_first_submit() -> None:
    class PlanHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/plan"
            prompt.focus()

    app = PlanHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))

        assert prompt.text == ""
        assert app._input_runtime_mode == RuntimeMode.PLAN
        assert not app._busy
        assert prompt.border_title == "规划模式"


# 功能：验证 Shift+Tab 只循环三种权限姿态且不会改变当前工作模式
# 设计：用有状态 fake IPC 合并局部更新，核对 authority 循环与 mode 独立性
async def test_shift_tab_cycles_and_persists_permission_modes() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        mode = "operate"
        profile = "ask"

        # 合并局部 authority 更新并返回完整快照
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            self.mode = str(params.get("mode", self.mode))
            self.profile = str(params.get("profile", self.profile))
            return {
                "snapshot": {
                    "mode": self.mode,
                    "profile": self.profile,
                }
            }

    class PermissionHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = PermissionHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        app._client = _FakeClient()  # type: ignore[assignment]
        app._session_id = "sess-permission"
        app._input_runtime_mode = RuntimeMode.OPERATE

        await app.action_cycle_permission_mode()
        assert app._authority_preset == "accept_edits"
        assert app._input_runtime_mode == RuntimeMode.OPERATE

        await app.action_cycle_permission_mode()
        assert app._authority_preset == "full_access"
        assert app._input_runtime_mode == RuntimeMode.OPERATE

        await app.action_cycle_permission_mode()
        assert app._authority_preset == "ask"
        assert app._input_runtime_mode == RuntimeMode.OPERATE

    assert [params["profile"] for _method, params in calls] == [
        "auto_review",
        "full_access",
        "ask",
    ]
    assert all("mode" not in params for _method, params in calls)


# 功能：验证 Tab 独立循环 Act、Operate、Plan 且不改变权限姿态
# 设计：使用有状态 fake IPC 返回完整快照，固定 Mode 与 Authority 的正交切换语义
async def test_tab_cycles_runtime_modes_without_changing_authority() -> None:
    calls: list[dict[str, object]] = []

    class _FakeClient:
        mode = "act"

        # 合并 mode 更新并保留 full access 姿态
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            assert method == "session.set_authority"
            calls.append(params)
            self.mode = str(params.get("mode", self.mode))
            return {"snapshot": {"mode": self.mode, "profile": "full_access"}}

    class ModeHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = ModeHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        app._client = _FakeClient()  # type: ignore[assignment]
        app._session_id = "sess-mode"
        app._authority_preset = "full_access"

        await app.action_cycle_runtime_mode()
        await app.action_cycle_runtime_mode()
        await app.action_cycle_runtime_mode()

    assert [params["mode"] for params in calls] == ["operate", "plan", "act"]
    assert all("profile" not in params for params in calls)
    assert app._authority_preset == "full_access"


# 功能：验证 /permissions 第一次 Enter 直接打开选择器且不会发送 agent 消息
# 设计：在真实 TUI 提交完整命令，检查输入禁用和选择器挂载，固定单次确认交互
async def test_permissions_command_opens_picker_on_first_submit() -> None:
    class PermissionHarness(CodeRookTuiApp):
        # 跳过 socket 连接并准备完整命令
        def on_mount(self) -> None:
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/permissions"
            prompt.focus()

    app = PermissionHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        await pilot.pause()

        assert prompt.disabled
        assert app.query_one(PermissionModePicker).has_focus
        assert not app._busy


# 功能：验证 /trust 和 /sandbox 使用独立状态并如实展示 Windows 无隔离后端
# 设计：让 fake IPC 只接收 trust 局部更新，再检查状态与输出，避免 mode/profile 被连带重置
async def test_trust_and_sandbox_commands_are_independent() -> None:
    calls: list[dict[str, object]] = []
    appended: list[Widget] = []

    class _FakeClient:
        # 返回包含四个独立维度的完整 authority 快照
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            assert method == "session.set_authority"
            calls.append(params)
            return {
                "snapshot": {
                    "mode": "operate",
                    "profile": "full_access",
                    "workspace_trust": params["workspace_trust"],
                    "sandbox": {
                        "available": False,
                        "kind": "windows_none",
                        "reason": "no OS isolation backend is available on Windows",
                    },
                }
            }

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-trust"
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._update_header = lambda _state: None  # type: ignore[method-assign]

    await app._set_workspace_trust(tui_app_module.WorkspaceTrust.TRUSTED)
    app._show_sandbox_status()

    assert calls == [
        {"session_id": "sess-trust", "workspace_trust": "trusted"}
    ]
    assert app._input_runtime_mode == RuntimeMode.OPERATE
    assert app._authority_preset == "full_access"
    output = "\n".join(str(widget.content) for widget in appended if isinstance(widget, Static))
    assert "Workspace trust" in output
    assert "trusted" in output
    assert "Sandbox" in output
    assert "unavailable" in output
    assert "windows_none" in output


# 功能：验证斜杠补全弹出时 Tab 仍优先完成命令而不是切换工作模式
# 设计：在真实 CodeRookTuiApp 输入部分命令并发送 Tab，防止全局 Mode 快捷键破坏既有单次提交交互
async def test_tab_keeps_slash_completion_priority() -> None:
    class SlashHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            self._slash_items = [("model", "switch model")]
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query("mo")
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/mo"
            prompt.focus()

    app = SlashHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert app.query_one("#prompt", ChatTextArea).text == "/model "
        assert app._input_runtime_mode == RuntimeMode.ACT


# 功能：验证 tasks、diff 和 context 视图读取 typed IPC 并产生可复制输出
# 设计：用单个 fake client 返回三类真实载荷，直接调用视图 worker，核对命令顺序和核心展示文本
async def test_high_frequency_views_render_core_results() -> None:
    calls: list[str] = []

    class _FakeClient:
        # 按命令返回任务、diff 或 context 载荷
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append(method)
            if method == "session.tasks":
                return {
                    "run_id": "run-1",
                    "tasks": [
                        {
                            "id": 1,
                            "subject": "修复权限",
                            "status": "in_progress",
                            "blocked_by": [],
                        }
                    ],
                }
            if method == "workspace.diff":
                return {
                    "payload": {
                        "files": [
                            {
                                "path": "src/auth.py",
                                "index_status": " ",
                                "worktree_status": "M",
                            }
                        ],
                        "additions": 3,
                        "deletions": 1,
                        "diff": "@@ -1 +1 @@\n-old\n+new",
                    }
                }
            return {
                "message_count": 12,
                "estimated_tokens": 2048,
                "run_count": 3,
                "last_run_id": "run-1",
            }

    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"

    await app._show_tasks()
    await app._show_diff()
    await app._show_context()

    rendered = "\n".join(
        str(getattr(widget, "content", "")) for widget in appended
    )
    assert calls == ["session.tasks", "workspace.diff", "session.context"]
    assert "修复权限" in rendered
    assert "src/auth.py" in rendered
    assert "estimated_tokens" in rendered
    assert any(isinstance(widget, Markdown) for widget in appended)


@pytest.mark.parametrize("command", ["/tasks", "/diff", "/rewind", "/context", "/turn"])
# 功能：验证五个高频视图命令第一次 Enter 就直接执行
# 设计：提交完整命令并截获 worker coroutine，检查输入清空、禁用和单次调度，防止回车两次回归
async def test_high_frequency_commands_execute_on_first_submit(command: str) -> None:
    scheduled: list[Coroutine[Any, Any, None]] = []

    class _FakeClient:
        # 提供已连接标记，本测试不实际调用 IPC
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            raise AssertionError("worker should be intercepted")

    class ViewHarness(CodeRookTuiApp):
        # 跳过 socket 连接并准备命令
        def on_mount(self) -> None:
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = command
            prompt.focus()

    app = ViewHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()

        # 截获 worker coroutine 以验证调度但不执行远程命令
        def schedule(
            coroutine: Coroutine[Any, Any, None],
            **_kwargs: object,
        ) -> None:
            scheduled.append(coroutine)

        app.run_worker = schedule  # type: ignore[method-assign]
        app._client = _FakeClient()  # type: ignore[assignment]
        app._session_id = "sess-1"
        prompt = app.query_one("#prompt", ChatTextArea)

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))

        assert prompt.text == ""
        assert prompt.disabled
        assert len(scheduled) == 1
        scheduled[0].close()


# 功能：验证运行中的普通输入会作为 run.steer 发送而不是误开新 turn
# 设计：保持 TUI busy 且绑定活动 run，用 fake IPC 和真实提交事件核对命令、内容和界面前缀
async def test_busy_input_steers_active_run() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        # 记录运行中纠偏命令
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            return {"queued": True, "run_id": params["run_id"]}

    class SteeringHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = SteeringHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        scheduled: list[asyncio.Task[None]] = []

        # 用 asyncio task 执行 TUI worker coroutine
        def schedule(
            coroutine: Coroutine[Any, Any, None],
            **_kwargs: object,
        ) -> asyncio.Task[None]:
            task = asyncio.create_task(coroutine)
            scheduled.append(task)
            return task

        app.run_worker = schedule  # type: ignore[method-assign]
        app._client = _FakeClient()  # type: ignore[assignment]
        app._session_id = "sess-1"
        app._active_run_id = "run-1"
        app._busy = True
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "不要删除旧接口"

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        await asyncio.gather(*scheduled)

        assert calls == [
            (
                "run.steer",
                {"run_id": "run-1", "content": "不要删除旧接口"},
            )
        ]
        assert prompt.text == ""
        assert prompt.border_title == "补充要求已发送"


# 功能：验证 Agent 结构化问题在 TUI 内显示，选择答案后恢复同一活动 run
# 设计：注入 user_question.asked 事件并按 Enter，核对 typed 回答命令和输入框恢复为纠偏状态
async def test_user_question_event_answers_without_starting_new_turn() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeClient:
        # 记录结构化问题回答命令
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            return {"answered": True}

    class QuestionHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = QuestionHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        scheduled: list[asyncio.Task[None]] = []

        # 用 asyncio task 执行回答 worker coroutine
        def schedule(
            coroutine: Coroutine[Any, Any, None],
            **_kwargs: object,
        ) -> asyncio.Task[None]:
            task = asyncio.create_task(coroutine)
            scheduled.append(task)
            return task

        app.run_worker = schedule  # type: ignore[method-assign]
        app._client = _FakeClient()  # type: ignore[assignment]
        app._session_id = "sess-1"
        app._active_run_id = "run-1"
        app._busy = True
        app._handle_event(
            {
                "type": "user_question.asked",
                "question_id": "question-1",
                "run_id": "run-1",
                "session_id": "sess-1",
                "question": "选择数据库？",
                "header": "数据库",
                "options": ["SQLite", "PostgreSQL"],
                "multi_select": False,
                "ts": "t",
            }
        )
        await pilot.pause()

        prompt = app.query_one("#prompt", ChatTextArea)
        assert prompt.disabled
        assert app.query_one(UserQuestionSelect).has_focus

        await pilot.press("enter")
        await pilot.pause()
        await asyncio.gather(*scheduled)

        assert calls == [
            (
                "user_question.respond",
                {"question_id": "question-1", "answer": "SQLite"},
            )
        ]
        assert app._pending_question_id is None
        assert not prompt.disabled
        assert prompt.border_title == "回答已发送 · Agent 继续执行"


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
    assert prompt.border_title == "任务已取消"
    assert states == ["interrupted"]


# 功能：验证未知事件类型不抛异常也不追加任何 widget
# 设计：发送 type 为 unknown 的事件，断言 appended 为空
def test_unknown_event_silently_ignored() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._append = lambda w: appended.append(w)  # type: ignore[method-assign]

    app._handle_event({"type": "some.unknown.type", "run_id": "r", "ts": "t"})
    assert appended == []


# 功能：验证实际 LLM route 事件更新 TUI 的 route、model 和运行状态
# 设计：直接投递不含密钥的 receipt 事件并截获 header 刷新，避免依赖真实 daemon
def test_route_selected_event_updates_observable_header_state() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999, model="old-model")
    states: list[str] = []
    app._update_header = lambda state: states.append(state)  # type: ignore[method-assign]

    app._handle_event(
        {
            "type": "llm.route_selected",
            "run_id": "run-1",
            "route_id": "openai-work",
            "wire_format": "openai_responses",
            "base_url_origin": "https://api.openai.com",
            "model": "gpt-test",
            "credential_source": "keyring",
            "ts": "t",
        }
    )

    assert app._route == "openai-work"
    assert app._model == "gpt-test"
    assert states == ["running"]

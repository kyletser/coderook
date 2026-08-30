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
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctorCheck, ProviderDoctorResult
from code_rook.core.llm.provider_presets import PROVIDER_PRESETS
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import get_route_preset
from code_rook.core.transport.socket_client import IpcError
from code_rook.tui import app as tui_app_module
from code_rook.tui.app import (
    ChatTextArea,
    CheckpointPicker,
    CodeRookTuiApp,
    CompletionItem,
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
from code_rook.tui.product import RunResultCard


# 功能：为 TUI 测试构造与候选 route/model 摘要绑定的完整 Doctor 结果
# 设计：按 route 声明能力生成通过或 unsupported 分项，使 fake 收据遵守生产提交门禁
def _doctor_success(route: object, credential_source: str = "keyring") -> ProviderDoctorResult:
    digest = getattr(route, "validation_digest")()
    supports_tools = bool(getattr(route, "supports_tools"))
    supports_parallel = bool(getattr(route, "supports_parallel_tools"))
    supports_images = bool(getattr(route, "supports_images"))
    return ProviderDoctorResult(
        status="ok",
        category="ok",
        route_id=str(getattr(route, "id")),
        message="all required capability checks passed",
        credential_source=credential_source,  # type: ignore[arg-type]
        readiness="verified",
        route_digest=digest,
        checked_at="2026-08-24T00:00:00+00:00",
        basic=ProviderDoctorCheck(status="passed", message="bounded request passed"),
        capabilities={
            "streaming": ProviderDoctorCheck(status="passed", message="stream passed"),
            "termination": ProviderDoctorCheck(status="passed", message="terminal passed"),
            "tool_calling": ProviderDoctorCheck(
                status="passed" if supports_tools else "unsupported",
                message="tool capability",
            ),
            "parallel_tools": ProviderDoctorCheck(
                status="passed" if supports_parallel else "unsupported",
                message="parallel capability",
            ),
            "images": ProviderDoctorCheck(
                status="passed" if supports_images else "unsupported",
                message="image capability",
            ),
        },
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
    assert _tool_action_text(
        "agent",
        start,
        finished=False,
        locale="en-US",
    ) == "Starting Worker inspect module"
    assert _tool_action_text(
        "agent",
        wait,
        finished=True,
        locale="en-US",
    ) == "Waited for Worker worker-1"


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


# 功能：edit_file 审批卡内嵌 old→new 的 diff 预览（W3.3）
# 设计：用两文本实例化审批面板，断言渲染文本含 DIFF 头与 +/- 行，验证审阅信息进卡
def test_permission_panel_embeds_edit_diff_preview() -> None:
    panel = PermissionSelect(
        "tool-1",
        "edit_file",
        "path='/ws/a.py'",
        {"path": "/ws/a.py", "old_text": "x = 1", "new_text": "x = 2"},
    )
    plain = render(panel._render_ui()).plain
    assert "DIFF" in plain
    assert "-x = 1" in plain
    assert "+x = 2" in plain


# 功能：非编辑类工具或不含 old/new 时审批卡不出现 diff 段
# 设计：bash 审批不含 diff 信息，断言无 DIFF 头，避免无关工具误带审阅块
def test_permission_panel_no_diff_for_bash() -> None:
    panel = PermissionSelect(
        "tool-1", "bash", "command='git status'", {"command": "git status"}
    )
    assert "DIFF" not in render(panel._render_ui()).plain


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


# 功能：验证 checkpoint 选择只生成预览，第二条显式 --yes 命令才真正恢复文件
# 设计：用 fake IPC 严格记录 preview/rewind 顺序与 digest，排除选择器 Enter 直接执行破坏性操作
async def test_rewind_requires_preview_then_explicit_confirmation() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Client:
        # 返回固定恢复预览并记录最终确认请求
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            if method == "session.rewind_preview":
                return {
                    "checkpoint_id": "cp-1",
                    "paths": ["src/a.py", "tests/test_a.py"],
                    "restorable": ["src/a.py"],
                    "already_restored": ["tests/test_a.py"],
                    "conflicts": [],
                    "state_digest": "a" * 64,
                }
            assert method == "session.rewind"
            return {"checkpoint_id": "cp-1", "restored": ["src/a.py"]}

    class _Picker:
        # 模拟已关闭的选择器而不依赖 Textual 挂载状态
        def remove(self) -> None:
            return None

    class _Message:
        picker = _Picker()
        checkpoint_id = "cp-1"

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _Client()  # type: ignore[assignment]
    app._session_id = "sess-1"
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    await app.on_checkpoint_picker_selected(_Message())  # type: ignore[arg-type]

    assert [method for method, _params in calls] == ["session.rewind_preview"]
    assert app._pending_rewind == {
        "session_id": "sess-1",
        "checkpoint_id": "cp-1",
        "state_digest": "a" * 64,
    }
    assert "/rewind --yes" in str(appended[-1].render())

    await app._confirm_rewind()

    assert [method for method, _params in calls] == [
        "session.rewind_preview",
        "session.rewind",
    ]
    assert calls[-1][1]["expected_digest"] == "a" * 64
    assert calls[-1][1]["confirmed"] is True
    assert app._pending_rewind is None


# 功能：验证含冲突的 Rewind 预览不会留下可供 --yes 使用的待确认摘要
# 设计：返回 conflict 路径并检查 pending 被清空，确保旧预览无法绕过冲突门禁
async def test_rewind_conflict_invalidates_pending_confirmation() -> None:
    class _Client:
        # 返回带冲突的恢复预览
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            del params
            assert method == "session.rewind_preview"
            return {
                "checkpoint_id": "cp-conflict",
                "paths": ["src/a.py"],
                "restorable": [],
                "already_restored": [],
                "conflicts": ["src/a.py"],
                "state_digest": "b" * 64,
            }

    class _Picker:
        # 模拟选择器移除
        def remove(self) -> None:
            return None

    class _Message:
        picker = _Picker()
        checkpoint_id = "cp-conflict"

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _Client()  # type: ignore[assignment]
    app._session_id = "sess-1"
    app._pending_rewind = {
        "session_id": "sess-1",
        "checkpoint_id": "old",
        "state_digest": "c" * 64,
    }
    app._append = lambda _widget: None  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    await app.on_checkpoint_picker_selected(_Message())  # type: ignore[arg-type]

    assert app._pending_rewind is None


# 功能：验证 Worker 审查完整展示摘要，应用成功只声明工作区改动且明确未提交未推送
# 设计：用同一 fake IPC 串联 review/apply 并检查参数、可复制 digest 与保守成功文案
async def test_worker_review_digest_and_explicit_apply_result() -> None:
    digest = "d" * 64
    calls: list[tuple[str, dict[str, object]]] = []

    class _Client:
        # 返回绑定同一 digest 的审查和应用结果
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            if method == "worker.review":
                preview_only = not bool(params.get("confirmed", False))
                return {
                    "worker_id": "worker-1",
                    "handoff_status": (
                        "pending_review" if preview_only else "reviewed_not_applied"
                    ),
                    "approved": not preview_only,
                    "applied": False,
                    "state_digest": digest,
                    "preview_only": preview_only,
                    "changed_files": ["src/a.py", "new.txt"],
                    "diff": (
                        "diff --git a/new.txt b/new.txt\n+untracked bytes\n"
                        if preview_only
                        else ""
                    ),
                    "diff_truncated": False,
                }
            assert method == "worker.apply"
            return {
                "worker_id": "worker-1",
                "handoff_status": "applied",
                "changed_files": ["src/a.py"],
                "state_digest": digest,
            }

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _Client()  # type: ignore[assignment]
    app._session_id = "sess-1"
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    await app._do_worker_review("worker-1", True)
    review_text = str(appended[-1].render())
    assert digest in review_text
    assert "untracked bytes" in review_text
    assert f"/workers review worker-1 approve {digest} --yes" in review_text

    await app._do_worker_review(
        "worker-1",
        True,
        confirmed=True,
        expected_digest=digest,
    )
    approved_text = str(appended[-1].render())
    assert f"/workers apply worker-1 {digest} --yes" in approved_text

    await app._do_worker_apply("worker-1", digest)
    apply_text = str(appended[-1].render())
    assert "改动已应用到当前工作区" in apply_text
    assert "未创建提交，未推送" in apply_text
    assert calls[-1] == (
        "worker.apply",
        {
            "session_id": "sess-1",
            "worker_id": "worker-1",
            "expected_digest": digest,
            "confirmed": True,
        },
    )


# 功能：验证 Worker apply 返回非 applied 状态时使用安全错误卡且不输出成功文案
# 设计：让 fake IPC 返回结构异常结果并替换安全错误入口，断言诊断类别可恢复且成功提示未出现
async def test_worker_apply_unexpected_result_fails_safely() -> None:
    class _Client:
        # 返回未应用状态模拟 daemon 合同异常
        async def send_command(
            self,
            _method: str,
            _params: dict[str, object],
        ) -> dict[str, object]:
            return {"worker_id": "worker-1", "handoff_status": "approved"}

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _Client()  # type: ignore[assignment]
    app._session_id = "sess-1"
    appended: list[Widget] = []
    safe_errors: list[str] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    # 捕获安全错误分类而不依赖完整 Textual 挂载
    def _capture_safe_error(
        category: str,
        _error: object = None,
        **_kwargs: object,
    ) -> str:
        safe_errors.append(category)
        return "diagnostic-test"

    app._show_safe_error = _capture_safe_error  # type: ignore[method-assign]

    await app._do_worker_apply("worker-1", "e" * 64)

    assert safe_errors == ["worker-apply"]
    assert appended == []


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


# 功能：验证 ModelPicker 支持模型 ID 子串搜索并展示活动 route 能力标签
# 设计：键入唯一子串过滤三项后回车，断言命中模型及 tools/images/thinking 标签均可见
async def test_model_picker_searches_ids_and_shows_route_capabilities() -> None:
    models = ["alpha-small", "beta-coder", "gamma-large"]

    class PickerHarness(App[None]):
        # 初始化模型搜索测试宿主
        def __init__(self) -> None:
            super().__init__()
            self.selected: list[str] = []

        # 挂载带能力标签的模型选择器
        def compose(self) -> ComposeResult:
            yield ModelPicker(
                models,
                "alpha-small",
                ("tools", "parallel-tools", "images", "thinking=high"),
            )

        # 记录过滤后的模型选择结果
        def on_model_picker_selected(self, message: ModelPicker.Selected) -> None:
            self.selected.append(message.model)

    app = PickerHarness()
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        await pilot.press("b", "e", "t", "a")
        picker = app.query_one(ModelPicker)
        plain = render(picker._render_ui()).plain

        assert "beta-coder" in plain
        assert "alpha-small" not in plain
        assert "tools · parallel-tools · images · thinking=high" in plain

        await pilot.press("enter")
        await pilot.pause()

    assert app.selected == ["beta-coder"]


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

    class _RouteDoctor:
        # 返回成功诊断，证明 TUI 只在 doctor 通过后提交配置事务
        async def check(self, route: object, credential: object) -> ProviderDoctorResult:
            del credential
            return _doctor_success(route)

    routes = RouteStore(tmp_path / "routes.json")
    app = ConfigHarness(
        "127.0.0.1",
        9999,
        provider="openai",
        model="gpt-5.6-sol",
        route_store=routes,
        credential_store=_Credentials(),  # type: ignore[arg-type]
        provider_doctor=_RouteDoctor(),  # type: ignore[arg-type]
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


# 功能：验证 Ollama 配置跳过 API Key 输入并以免密 route 完成 Doctor 后保存
# 设计：驱动真实 Provider 与模型选择器，注入本地模型发现和诊断器且令凭据写入直接失败
async def test_inline_local_provider_flow_requires_no_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_discover(provider: object, api_key: str) -> list[str]:
        assert getattr(provider, "id") == "ollama"
        assert api_key == ""
        return ["qwen3-coder"]

    monkeypatch.setattr(tui_app_module, "discover_models", fake_discover)

    class ConfigHarness(CodeRookTuiApp):
        # 挂载隔离配置界面而不连接真实 daemon
        def on_mount(self) -> None:
            self._slash_items = self._build_slash_items()
            self._session_id = "session-local"
            self.query_one("#prompt", ChatTextArea).focus()

    class _Credentials:
        # 返回免密解析结果并拒绝任何意外密钥持久化
        def resolve(self, _credential_ref: str) -> object:
            from code_rook.core.llm.credentials import CredentialResolution

            return CredentialResolution(source="missing")

        # 本地免密 route 不应调用凭据写入
        def save(self, _route_id: str, _api_key: str) -> str:
            raise AssertionError("local provider must not persist an API key")

    class _RouteDoctor:
        # 接受免密本地探测并检查 route 保留 catalog 能力元数据
        async def check(self, route: object, credential: object) -> ProviderDoctorResult:
            assert getattr(route, "catalog_id") == "ollama"
            assert getattr(route, "credential_required") is False
            assert getattr(credential, "value") is None
            return _doctor_success(route, "missing")

    routes = RouteStore(tmp_path / "routes.json")
    app = ConfigHarness(
        "127.0.0.1",
        9999,
        route_store=routes,
        credential_store=_Credentials(),  # type: ignore[arg-type]
        provider_doctor=_RouteDoctor(),  # type: ignore[arg-type]
    )
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "/config"
        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        await pilot.pause()
        for _ in range(7):
            await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        assert not app.query(ConfigApiKeyPrompt)
        assert app.query_one(ModelPicker)
        await pilot.press("enter")
        await pilot.pause()

    active = routes.active()
    assert active is not None
    assert active.id == "ollama"
    assert active.credential_required is False
    assert active.credential_ref == "none:ollama"
    assert app._model == "qwen3-coder"


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
            yield SlashCompleteWidget([CompletionItem("model", "switch model")])
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
            yield SlashCompleteWidget([CompletionItem("model", "switch model")])
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


# 功能：验证 TUI 稳定命令包含常用入口且默认隐藏 Labs 命令
# 设计：直接读取候选列表，同时断言实验命令不会污染首次使用界面
def test_tui_builtin_commands_include_model_picker() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    items = {
        item.name: item.description
        for item in app._build_slash_items()  # type: ignore[attr-defined]
    }

    assert items["help"] == "显示键位与全部命令"
    assert items["model"] == "查看或切换模型"
    assert items["provider"] == "查看或切换 Provider route"
    assert items["doctor"] == "诊断活动 Provider route"
    assert items["rename"] == "重命名当前会话：/rename <标题>"
    assert items["fork"] == "复制当前会话为分支：/fork [标题]"
    assert items["export"] == "导出当前会话：/export [md|json]"
    assert items["delete"] == "删除当前会话（需 --yes 确认）"
    assert items["plan"] == "只读规划并审阅后再实施：/plan [任务]"
    assert items["permissions"] == "查看或切换权限模式"
    assert items["tasks"] == "查看最近一次 run 的任务"
    assert items["workers"] == "查看、审查或应用持久 Worker"
    assert "workflow" not in items
    assert "hooks" not in items
    assert items["diff"] == "查看工作区改动"
    assert items["rewind"] == "预览并二次确认安全恢复点"
    assert items["context"] == "查看上下文占用与用量"
    assert items["skills"] == "列出、查看、安装或删除 skills"


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


# 功能：验证 TUI 的 Provider 与模型切换都在 Doctor 通过后更新 RouteStore
# 设计：注入固定凭据和摘要绑定 Doctor，依次等待两个受检动作并核对活动 route/model 与收据
async def test_tui_route_and_model_switch_update_route_store(tmp_path: Path) -> None:
    class _Credentials:
        # 为两个远程 route 提供隔离测试凭据
        def resolve(self, _credential_ref: str) -> object:
            from code_rook.core.llm.credentials import CredentialResolution

            return CredentialResolution(value="test-secret", source="file")

    class _Doctor:
        # 返回与每次候选 model 动态摘要绑定的基础成功结果
        async def check(self, route: object, _credential: object) -> ProviderDoctorResult:
            return _doctor_success(route, "file")

    routes = RouteStore(tmp_path / "routes.json")
    routes.add(get_route_preset("anthropic"), activate=True)
    routes.add(get_route_preset("openai"))
    app = CodeRookTuiApp(
        "127.0.0.1",
        9999,
        route_store=routes,
        credential_store=_Credentials(),  # type: ignore[arg-type]
        provider_doctor=_Doctor(),  # type: ignore[arg-type]
    )
    appended: list[Widget] = []
    states: list[str] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._update_header = lambda state: states.append(state)  # type: ignore[method-assign]

    await app._select_provider_route_checked("openai")
    await app._select_route_model_checked("gpt-route-test")

    active = routes.active()
    assert active is not None
    assert active.id == "openai"
    assert active.model == "gpt-route-test"
    assert active.has_current_doctor_receipt()
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
        async def check(self, route: object, _credential: object) -> ProviderDoctorResult:
            return _doctor_success(route, "file").model_copy(update={"http_status": 200})

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
    assert "all required capability checks passed" in rendered
    assert "streaming=passed" in rendered
    assert "termination=passed" in rendered
    assert "credential=file" in rendered
    assert "top-secret" not in rendered
    assert restored == [True]


# 功能：验证 TUI 内建命令包含会话选择器与新建会话入口
# 设计：直接读取候选列表的描述文本，不挂载界面也不依赖 socket
def test_tui_builtin_commands_include_session_picker_and_new_session() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    items = {
        item.name: item.description
        for item in app._build_slash_items()  # type: ignore[attr-defined]
    }

    assert items["sessions"] == "打开会话选择器（输入即过滤）"
    assert items["new"] == "新建会话"


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
    app._locale = "en-US"
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


# 功能：验证结果卡在 Runtime receipt 延迟投影时按有界阶梯等待而非过早回退
# 设计：前三次返回未完成收据、第四次返回 finished_at，并替换 sleep 记录延迟避免真实等待
async def test_run_result_waits_for_delayed_authoritative_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []

    # 模拟 Runtime 投影在第四次查询才完成
    async def inspect(_client: object, turn_id: str) -> dict[str, object]:
        nonlocal attempts
        assert turn_id == "run-delayed"
        attempts += 1
        if attempts < 4:
            return {"turn": {}, "receipt": {}}
        return {
            "turn": {"status": "completed"},
            "receipt": {
                "status": "completed",
                "started_at": "2026-08-24T00:00:00+00:00",
                "finished_at": "2026-08-24T00:00:01+00:00",
                "verification": [],
                "files_changed": [],
            },
        }

    # 记录阶梯等待但不消耗测试墙钟时间
    async def no_wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(tui_app_module.ipc_actions, "inspect_turn", inspect)
    monkeypatch.setattr(tui_app_module.asyncio, "sleep", no_wait)
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = object()  # type: ignore[assignment]
    app._session_id = "sess-1"
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    event = {
        "type": "run.finished",
        "run_id": "run-delayed",
        "status": "success",
        "steps": 1,
        "ts": "2026-08-24T00:00:01+00:00",
        "_tui_session_id": "sess-1",
    }

    await app._render_run_result(event)

    assert attempts == 4
    assert delays == [0.05, 0.1, 0.2]
    assert len(appended) == 1
    assert isinstance(appended[0], RunResultCard)


# 功能：验证 run.finished failed 追加轻量失败摘要
# 设计：与成功态对称检查失败图标、步骤数和原因，保留必要诊断而不显示内部运行头
def test_run_finished_failed_shows_red() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._locale = "en-US"
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


# 功能：验证运行中工具能展开并原位刷新有界输出尾部与耗时
# 设计：先挂载未完成工具，再连续注入 progress，确保不会生成第二张卡或提前显示成功
async def test_running_tool_progress_updates_expanded_detail() -> None:
    block = ToolCallBlock(
        "Bash",
        {"action": "run", "command": "slow command"},
        presentation={
            "action": "run_command",
            "kind": "terminal",
            "command": "slow command",
            "supports_live_output": True,
        },
    )

    class ProgressToolHarness(App[None]):
        # 挂载运行中工具以验证 progress 原位刷新
        def compose(self) -> ComposeResult:
            yield block

    app = ProgressToolHarness()
    async with app.run_test(size=(80, 20)) as pilot:
        block.set_progress("phase 1", 1100)
        block.set_expanded(True)
        await pilot.pause()
        block.set_progress("phase 2", 2200)
        await pilot.pause()

        detail = str(block.query_one(".detail-content", Static).content)
        assert block._finished is False  # type: ignore[attr-defined]
        assert "phase 2" in detail
        assert "运行中 · 2200 ms" in detail
        assert "2.2s" in render(block._summary()).plain  # type: ignore[attr-defined]


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


# 功能：验证同一 step 的多个工具默认折叠为一个紧凑活动组
# 设计：先挂载再动态追加第二个，检查聚合标题、子项数量和点击展开行为
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
        assert "2 项" in render(str(header.content)).plain
        assert len(group.query(ToolCallBlock)) == 2
        assert "collapsed" in group.classes

        event = type("Click", (), {"widget": header})()
        group.on_click(event)  # type: ignore[arg-type]
        assert "collapsed" not in group.classes


# 功能：验证工具失败时活动组保持紧凑且展开后提供复制与安全重试入口
# 设计：使用带语义 Presentation 的失败工具更新父组，先检查折叠摘要，再手动展开读取恢复提示
async def test_tool_failure_stays_collapsed_with_recovery_actions() -> None:
    block = ToolCallBlock(
        "Bash",
        {"action": "run", "command": "exit 1"},
        presentation={
            "action": "run_command",
            "kind": "terminal",
            "command": "exit 1",
        },
    )
    group = ToolStepGroup(1)
    group.add_tool(block)

    class FailureGroupHarness(App[None]):
        # 挂载失败活动组以验证父子状态联动
        def compose(self) -> ComposeResult:
            yield group

    app = FailureGroupHarness()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        block.set_result("command failed", 1600, is_error=True)
        await pilot.pause()
        assert "collapsed" in group.classes

        header_widget = group.query_one(".step-header", Static)
        group.on_click(type("Click", (), {"widget": header_widget})())  # type: ignore[arg-type]
        block.set_expanded(True)
        await pilot.pause()

        header = render(str(group.query_one(".step-header", Static).content)).plain
        detail = str(block.query_one(".detail-content", Static).content)
        assert "collapsed" not in group.classes
        assert "1 项失败" in header
        assert "1.6s" in header
        assert "C 复制错误 · R 填入重试建议" in detail


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

        # 输入历史记录在真实 ChatTextArea 上，fake 桩无需落盘
        def record_history(self, _text: str) -> None:
            return None

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

    # 绕过本测试范围外的 Provider 探测，只观察消息提交交互
    async def ready() -> bool:
        return True

    app._ensure_task_ready = ready  # type: ignore[method-assign]

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
            if method == "plan.respond":
                app._handle_event(
                    {
                        "type": "plan.resolved",
                        "session_id": "sess-plan",
                        "run_id": "run-plan",
                        "decision": params.get("decision", "approve"),
                        "revision": "",
                        "ts": "t",
                    }
                )
                return {
                    "session_id": "sess-plan",
                    "run_id": "run-plan",
                    "decision": params.get("decision", "approve"),
                    "status": "resolved",
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

        # 绕过本测试范围外的 Provider 探测，只观察 Plan/Act 状态机
        async def ready() -> bool:
            return True

        app._ensure_task_ready = ready  # type: ignore[method-assign]
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
                    "display_content": "inspect authentication",
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
        assert calls[1] == ("goal.get", {"session_id": "sess-plan"})

        await pilot.press("enter")
        await pilot.pause()
        await asyncio.gather(*scheduled)

        assert calls[2] == (
            "plan.respond",
            {
                "session_id": "sess-plan",
                "run_id": "run-plan",
                "decision": "approve",
                "revision": "",
            },
        )
        assert calls[3][0] == "session.set_authority"
        assert calls[4][0] == "session.send_message"
        assert calls[4][1]["runtime_mode"] == "act"
        assert str(calls[4][1]["content"]).startswith("Implement the approved plan")
        assert "Original user request:\ninspect authentication" in str(
            calls[4][1]["content"]
        )


# 功能：验证 plan.respond 失败时审阅面板与 pending 状态保持可重试且不会启动 Act
# 设计：让 fake IPC 在 durable 决定边界报错，检查控件仍挂载、恢复焦点且没有后续命令
async def test_plan_response_failure_keeps_review_pending() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _FailingClient:
        # 在计划决定命令处模拟 Core 拒绝或持久化失败
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            raise IpcError(-32602, "plan response could not be persisted")

    class PlanHarness(CodeRookTuiApp):
        # 跳过 socket worker并聚焦输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = PlanHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app._client = _FailingClient()  # type: ignore[assignment]
        app._session_id = "sess-plan"
        app._handle_event(
            {
                "type": "plan.ready",
                "session_id": "sess-plan",
                "run_id": "run-plan",
                "request": "inspect auth",
                "plan": "1. Inspect",
                "ts": "t",
            }
        )
        await pilot.pause()
        review = app.query_one(PlanReview)

        await app.on_plan_review_decided(PlanReview.Decided(review, "approve"))
        await pilot.pause()

        assert calls == [
            (
                "plan.respond",
                {
                    "session_id": "sess-plan",
                    "run_id": "run-plan",
                    "decision": "approve",
                    "revision": "",
                },
            )
        ]
        assert app.query_one(PlanReview) is review
        assert review.has_focus
        assert not review.disabled
        assert app._plan_review_pending
        assert app._plan_run_id == "run-plan"


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


# 功能：验证 /trust 和 /sandbox 使用独立状态并如实展示 Windows 降级隔离
# 设计：让 fake IPC 只接收 trust 局部更新，再检查 DEGRADED 语义，避免把审批链误称为系统沙箱
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
    app._locale = "en-US"
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
    assert "DEGRADED" in output
    assert "windows_none" in output
    assert "not an OS sandbox" in output


# 功能：验证 Windows ACL 能力在 TUI 中明确显示 PARTIAL 和读网边界
# 设计：直接渲染 capability 卡片并检查用户文案，防止 available 布尔值把部分隔离误标为 ENFORCED
def test_windows_acl_sandbox_status_is_rendered_as_partial() -> None:
    appended: list[Widget] = []
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._locale = "zh-CN"
    app._sandbox = {
        "available": True,
        "kind": "windows_acl",
        "reason": "restricted-token probe succeeded",
    }
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._show_sandbox_status()

    output = "\n".join(
        str(widget.content) for widget in appended if isinstance(widget, Static)
    )
    assert "PARTIAL" in output
    assert "windows_acl" in output
    assert "读取和网络不隔离" in output


# 功能：验证首次连接在无模型与无 OS 沙箱时显示非阻塞的可执行空状态
# 设计：截获 transcript 输出并重复调用启动状态，断言两类提示各出现一次且不会强制打开配置流程
def test_startup_state_explains_no_model_and_degraded_sandbox_once(
    tmp_path: Path,
) -> None:
    appended: list[Widget] = []
    app = CodeRookTuiApp(
        "127.0.0.1",
        9999,
        route_store=RouteStore(tmp_path / "routes.json"),
        credential_store=CredentialStore(tmp_path / "credentials.json"),
    )
    app._sandbox = {
        "available": False,
        "kind": "windows_none",
        "reason": "no OS isolation backend is available on Windows",
    }
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._show_startup_state()
    app._show_startup_state()

    output = "\n".join(str(widget.content) for widget in appended if isinstance(widget, Static))
    assert output.count("欢迎使用 CodeRook") == 1
    assert "浏览会话和管理功能已经可用" in output
    assert output.count("Sandbox DEGRADED") == 1
    assert "windows_none" in output


# 功能：验证连接故障提示按类型去重且恢复后允许再次报告
# 设计：直接驱动产品提示接口模拟拒绝连接、恢复和再次断线，固定重试循环不会刷屏的交互契约
def test_connection_problem_notice_deduplicates_until_recovery() -> None:
    appended: list[Widget] = []
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._show_connection_problem("unreachable", "cannot connect")
    app._show_connection_problem("unreachable", "cannot connect")
    app._clear_connection_problems()
    app._show_connection_problem("unreachable", "cannot connect")

    output = "\n".join(str(widget.content) for widget in appended if isinstance(widget, Static))
    assert output.count("操作未完成") == 2
    assert "诊断 ID" in output
    assert "cannot connect" not in output


# 功能：验证 TUI 明确区分新建、恢复与断线重连会话，并避免首次提示重复
# 设计：直接收集产品提示控件，重复发送新建事件后再发送重连事件，覆盖去重和可见恢复反馈
def test_session_ready_notice_describes_context_source() -> None:
    appended: list[Widget] = []
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._locale = "en-US"
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._show_session_ready("created", "sess-1", "", 0)
    app._show_session_ready("created", "sess-1", "", 0)
    app._show_session_ready("resumed", "sess-2", "修复登录", 4)
    app._show_session_ready("reconnected", "sess-2", "修复登录", None)

    output = "\n".join(str(widget.content) for widget in appended if isinstance(widget, Static))
    assert output.count("New session") == 1
    assert "Session resumed" in output
    assert "4 history message(s)" in output
    assert "Session reconnected" in output


# 功能：验证斜杠补全弹出时 Tab 仍优先完成命令而不是切换工作模式
# 设计：在真实 CodeRookTuiApp 输入部分命令并发送 Tab，防止全局 Mode 快捷键破坏既有单次提交交互
async def test_tab_keeps_slash_completion_priority() -> None:
    class SlashHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框
        def on_mount(self) -> None:
            self._slash_items = [CompletionItem("model", "switch model")]
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


# 功能：验证输入 /命令 + 前缀参数时 Tab 把参数补全为候选且只推进一次
# 设计：复用 CodeRookTuiApp 真实 Tab 链路，防止优先级快捷键与消息路径产生双重补全
async def test_tab_completes_command_arg() -> None:
    class SlashArgHarness(CodeRookTuiApp):
        # 跳过 socket 连接并聚焦输入框，输入 /mode 的部分参数
        def on_mount(self) -> None:
            self._slash_items = [CompletionItem("mode", "查看或切换工作模式", "plan|act|operate")]
            prompt = self.query_one("#prompt", ChatTextArea)
            prompt.text = "/mode a"
            prompt.focus()

    app = SlashArgHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert app.query_one("#prompt", ChatTextArea).text == "/mode act"
        # 工作模式未被参数补全占用，仍停留在初始 Act
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
    assert calls == [
        "session.tasks",
        "workspace.diff",
        "session.context",
        "turn.inspect",
        "session.context",
    ]
    assert "修复权限" in rendered
    assert "src/auth.py" in rendered
    assert "estimated_tokens" in rendered
    assert "改动中心" in rendered


@pytest.mark.parametrize(
    "command",
    ["/tasks", "/changes", "/diff", "/rewind", "/context", "/turn"],
)
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


# 功能：验证 run 尚未返回 ID 时提交的纠偏草稿不会被清空
# 设计：保持 busy 但不设置 active_run_id，直接提交 fake 输入并断言文本原样保留且未调度 IPC
async def test_starting_run_preserves_steer_draft() -> None:
    appended: list[Widget] = []

    class _FakeArea:
        text = "不要修改数据库"

        # fake 输入历史无需持久化
        def record_history(self, _content: str) -> None:
            return None

    class _FakeEvent:
        value = "不要修改数据库"
        text_area = _FakeArea()

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._busy = True
    app._client = object()  # type: ignore[assignment]
    app._active_run_id = None
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    await app.on_chat_text_area_submitted(_FakeEvent())  # type: ignore[arg-type]

    assert _FakeEvent.text_area.text == "不要修改数据库"
    assert "当前输入已保留" in str(getattr(appended[0], "content", ""))


# 功能：验证首条任务发送失败时恢复文字草稿、图片附件和可交互输入状态
# 设计：让 fake IPC 抛出 RuntimeError，并用空输入框确认恢复逻辑不会依赖真实 Textual worker
async def test_send_failure_restores_draft_and_attachments() -> None:
    appended: list[Widget] = []
    states: list[str] = []
    attachment = {"sha256": "abc", "media_type": "image/png", "size": 3}

    class _FailingClient:
        # 模拟 Core 在确认消息前断开
        async def send_command(
            self,
            _method: str,
            _params: dict[str, object],
        ) -> dict[str, object]:
            raise RuntimeError("connection closed")

    class _FakePrompt:
        text = ""
        disabled = False
        read_only = False
        border_title = "running"
        focused = False

        # 记录恢复后输入框重新获得焦点
        def focus(self) -> None:
            self.focused = True

    app = CodeRookTuiApp("127.0.0.1", 9999)
    prompt = _FakePrompt()
    app._client = _FailingClient()  # type: ignore[assignment]
    app._session_id = "sess-1"
    app._busy = True
    app._prompt = lambda: prompt  # type: ignore[method-assign]
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._update_header = lambda state: states.append(state)  # type: ignore[method-assign]

    await app._do_send_message("修复登录", attachments=[attachment])

    assert not app._busy
    assert prompt.text == "修复登录"
    assert prompt.border_title == "发送失败 · 原输入已恢复"
    assert prompt.focused
    assert app._pending_image_attachments == [attachment]
    assert states == ["ready"]
    rendered = str(getattr(appended[-1], "content", ""))
    assert "诊断 ID" in rendered
    assert "connection closed" not in rendered


# 功能：验证运行中纠偏发送失败时恢复纠偏草稿且不终止原 run
# 设计：复用失败 IPC 边界并保持 busy，断言用户可直接再次按 Enter 而不会丢失纠偏内容
async def test_steer_failure_restores_draft_without_ending_run() -> None:
    class _FailingClient:
        # 模拟 steer 命令未被 Core 接收
        async def send_command(
            self,
            _method: str,
            _params: dict[str, object],
        ) -> dict[str, object]:
            raise RuntimeError("queue unavailable")

    class _FakePrompt:
        text = ""
        disabled = False
        read_only = False
        border_title = "running"
        focused = False

        # 记录纠偏草稿恢复后的焦点
        def focus(self) -> None:
            self.focused = True

    app = CodeRookTuiApp("127.0.0.1", 9999)
    prompt = _FakePrompt()
    app._client = _FailingClient()  # type: ignore[assignment]
    app._busy = True
    app._prompt = lambda: prompt  # type: ignore[method-assign]
    app._append = lambda _widget: None  # type: ignore[method-assign]

    await app._do_steer("run-1", "保留旧接口")

    assert app._busy
    assert prompt.text == "保留旧接口"
    assert prompt.border_title == "纠偏发送失败 · 输入已恢复"
    assert prompt.focused


# 功能：验证 Goal 创建在断线竞态中恢复为可重试命令并解除 busy
# 设计：在 worker 执行前移除 client，确认早退路径不会留下永久忙碌状态或吞掉目标文本
async def test_goal_create_disconnect_restores_command_draft() -> None:
    states: list[str] = []

    class _FakePrompt:
        text = ""
        disabled = False
        read_only = False
        border_title = "running"

        # 断线状态下不应触发焦点，但保留兼容接口
        def focus(self) -> None:
            raise AssertionError("disconnected prompt must stay disabled")

    app = CodeRookTuiApp("127.0.0.1", 9999)
    prompt = _FakePrompt()
    app._client = None
    app._session_id = "sess-1"
    app._busy = True
    app._prompt = lambda: prompt  # type: ignore[method-assign]
    app._update_header = lambda state: states.append(state)  # type: ignore[method-assign]

    await app._do_goal_command("create", "完成发布检查")

    assert not app._busy
    assert prompt.text == "/goal 完成发布检查"
    assert prompt.disabled
    assert prompt.border_title == "连接中 · Goal 输入已恢复"
    assert states == ["disconnected"]


# 功能：验证 Goal 状态摘要展示当前轮次、累计预算、已有证据、未完成标准和恢复原因
# 设计：构造暂停且需确认的完整 Goal 投影，核对稳定产品面要求的所有可审计字段
def test_goal_summary_shows_progress_evidence_and_pause_reason() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)

    rendered = app._format_goal_summary(
        {
            "id": "goal-123456789abc",
            "status": "paused",
            "objective": "Ship v1",
            "tokens_used": 900,
            "token_budget": 1200,
            "elapsed_ms": 125000,
            "max_wall_seconds": 1800,
            "linked_run_ids": ["run-1", "run-2"],
            "auto_turns_used": 1,
            "max_auto_turns": 3,
            "completion_criteria": ["tests pass", "docs aligned"],
            "completion_evidence": [
                {
                    "kind": "verified-run",
                    "reference": "run://run-2",
                    "summary": "tests pass",
                    "covered_criteria": ["tests pass"],
                }
            ],
            "paused_reason": "daemon_restart_confirmation_required",
            "status_reason": "daemon restarted",
            "paused_needs_confirmation": True,
        }
    )

    assert "round=2" in rendered
    assert "auto=1/3" in rendered
    assert "tokens=900/1200" in rendered
    assert "elapsed=125s/1800s" in rendered
    assert "paused_needs_confirmation=yes" in rendered
    assert "已有证据" in rendered and "tests pass" in rendered
    assert "未完成标准" in rendered and "docs aligned" in rendered
    assert "daemon_restart_confirmation_required" in rendered
    assert "/goal resume" in rendered


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
# 功能：验证 stage 后强制展示包含既有 index 内容的完整 staged 审查并保存 staged 令牌
# 设计：让 Core 返回 one 与 two 的最终 staged Diff，断言 TUI 不只显示文件名而会渲染不可跳过的内容视图
async def test_stage_changes_presents_authoritative_staged_review() -> None:
    staged_digest = "b" * 64
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeClient:
        # 返回包含既有 staged 文件与新选择文件的最终权威审查
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, dict(params)))
            if method == "workspace.stage":
                return {
                    "payload": {
                        "scope": "staged",
                        "state_digest": staged_digest,
                        "files": [
                            {
                                "path": "one.txt",
                                "index_status": "M",
                                "worktree_status": " ",
                                "review_complete": True,
                                "additions": 1,
                                "deletions": 1,
                            },
                            {
                                "path": "two.txt",
                                "index_status": "M",
                                "worktree_status": " ",
                                "review_complete": True,
                                "additions": 1,
                                "deletions": 1,
                            },
                        ],
                        "file_count": 2,
                        "additions": 2,
                        "deletions": 2,
                        "diff_truncated": False,
                        "diff": (
                            "diff --git a/one.txt b/one.txt\n"
                            "--- a/one.txt\n+++ b/one.txt\n"
                            "@@ -1 +1 @@\n-base\n+preexisting staged A\n"
                            "diff --git a/two.txt b/two.txt\n"
                            "--- a/two.txt\n+++ b/two.txt\n"
                            "@@ -1 +1 @@\n-base\n+new staged C\n"
                        ),
                    }
                }
            if method == "session.context":
                return {"last_run_id": None}
            raise AssertionError(method)

    app = CodeRookTuiApp("127.0.0.1", 9999)
    appended: list[Widget] = []
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"
    app._change_state_digest = "a" * 64
    app._change_review_scope = "all"
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    await app._stage_changes(["two.txt"])

    rendered = "\n".join(str(getattr(widget, "content", "")) for widget in appended)
    assert calls[0] == (
        "workspace.stage",
        {
            "session_id": "sess-1",
            "paths": ["two.txt"],
            "expected_digest": "a" * 64,
            "confirmed": True,
        },
    )
    assert "preexisting staged A" in rendered
    assert "two.txt" in rendered
    assert app._change_state_digest == staged_digest
    assert app._change_review_scope == "staged"


# 功能：验证 all-scope 审查令牌不能由 TUI 直接用于 commit
# 设计：用禁止调用的 fake client 和安全错误捕获证明用户必须先 stage 并查看最终 staged Diff
async def test_commit_changes_rejects_unshown_all_scope_review() -> None:
    appended: list[Widget] = []

    class _FakeClient:
        # 任何 IPC 调用都表示客户端错误地接受了 all-scope 令牌
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise AssertionError((method, params))

    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = _FakeClient()  # type: ignore[assignment]
    app._session_id = "sess-1"
    app._change_state_digest = "a" * 64
    app._change_review_scope = "all"
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    await app._commit_changes("fix: reviewed")

    rendered = "\n".join(str(getattr(widget, "content", "")) for widget in appended)
    assert "/diff" in rendered
    assert "/stage" in rendered


# 功能：验证 v1.1 顶栏只保留仓库、模型和权威运行阶段而不泄露内部 ID
# 设计：直接设置阶段投影并以 80 列渲染，检查关键状态可见且 session/route 标签被移除
def test_focused_header_uses_authoritative_run_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    app = CodeRookTuiApp("127.0.0.1", 9999, model="deepseek-chat")
    app._session_id = "session-secret"
    app._route = "route-internal"
    app._run_phase = "verifying"
    app._run_phase_current = 6
    app._run_phase_total = 8

    rendered = app._render_responsive_header("running", 80)

    assert "deepseek-chat" in rendered
    assert "6/8" in rendered
    assert "session-secret" not in rendered
    assert "route-internal" not in rendered


# 功能：验证 @文件 仅注入工作区相对引用和有界读取约束而不附加文件全文
# 设计：创建含敏感正文的真实文件，解析后检查路径存在、正文缺失且越界引用被忽略
def test_file_reference_is_bounded_path_not_full_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "auth.py").write_text("SECRET_FULL_FILE_CONTENT", encoding="utf-8")
    app = CodeRookTuiApp("127.0.0.1", 9999)

    augmented = app._augment_file_references("review @auth.py", "review @auth.py")

    assert '"auth.py"' in augmented
    assert "SECRET_FULL_FILE_CONTENT" not in augmented
    assert "do not inject entire files" in augmented


# 功能：验证普通提交和排队提交共享同一套显式 Shell 语义转换
# 设计：直接调用统一转换入口，检查命令原文保留且生成正常工具管线约束，避免队列绕过快捷语法
def test_prepare_model_content_expands_explicit_shell_command() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)

    prepared = app._prepare_model_content("!pytest -q")

    assert "exact shell command" in prepared
    assert "pytest -q" in prepared
    assert "permission and sandbox tool pipeline" in prepared


# 功能：验证恢复历史会将工具结果错误映射为失败而不是成功
# 设计：构造相邻 tool_use/tool_result 消息并捕获简洁历史块，固定重启后不得绿勾误报的用户契约
def test_history_tool_error_is_rendered_as_failure() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._locale = "zh-CN"
    appended: list[Widget] = []
    app._append = lambda widget: appended.append(widget)  # type: ignore[method-assign]

    app._append_history(
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-failed",
                        "name": "Bash",
                        "input": {"command": "wmic cpu get name"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-failed",
                        "content": "command not found",
                        "is_error": True,
                    }
                ],
            },
        ]
    )

    output = "\n".join(str(getattr(widget, "content", "")) for widget in appended)
    assert "── 历史会话 ──" in output
    assert "[red]×[/red]" in output
    assert "执行失败" in output
    assert "[green]✓[/green]" not in output


# 功能：验证沙箱能力尚未返回时状态栏显示检查中而不误报 Windows 无沙箱
# 设计：在真实 Textual 挂载后刷新默认状态，同时覆盖连接初期的用户可见文案和控件更新
async def test_status_bar_waits_for_sandbox_detection() -> None:
    class StatusHarness(CodeRookTuiApp):
        # 挂载后不连接 Core，只验证默认能力探测状态
        def on_mount(self) -> None:
            self._update_status_bar()

    app = StatusHarness("127.0.0.1", 9999, locale="zh-CN")
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        status = str(app.query_one("#status-bar", Static).content)

    assert "Sandbox 检查中" in status
    assert "Windows 无 OS 沙箱" not in status

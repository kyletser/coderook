from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rich.markup import render
from textual.containers import VerticalScroll
from textual.widgets import Static

from code_rook.core.transport.socket_client import IpcError
from code_rook.tui import app as tui_app_module
from code_rook.tui.app import (
    ChatTextArea,
    CodeRookTuiApp,
    SessionPicker,
    _derive_session_title,
    _load_input_history,
    _save_input_history_entry,
)
from code_rook.tui.connection import TuiConnection
from code_rook.tui.widgets import input as widgets_input


class _FakeClient:
    """记录 send_command 调用并按命令名返回预设结果的假 IPC 客户端。"""

    # 初始化调用记录与预设响应表
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._responses = responses or {}

    # 记录命令并返回预设结果，未预设时返回空字典
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return self._responses.get(method, {})


# 功能：验证输入历史的磁盘读写往返与坏行容错
# 设计：把历史文件路径重定向到临时目录，写入正常行与损坏行后断言加载只保留可解析条目
def test_input_history_roundtrip_tolerates_corrupt_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history_file = tmp_path / "tui-history.jsonl"
    monkeypatch.setattr(widgets_input, "_input_history_path", lambda: history_file)

    _save_input_history_entry("第一条输入")
    _save_input_history_entry("  ")
    history_file.write_text(
        history_file.read_text(encoding="utf-8") + "{not-json}\n",
        encoding="utf-8",
    )
    _save_input_history_entry("第二条输入")

    assert _load_input_history() == ["第一条输入", "第二条输入"]
    assert json.loads(history_file.read_text(encoding="utf-8").splitlines()[0]) == {
        "text": "第一条输入"
    }


# 功能：验证 ↑/↓ 回溯历史：从最新向上、到底后恢复草稿
# 设计：直接驱动 ChatTextArea 的历史方法并替换持久化函数，覆盖首入保存草稿与退出回溯两个分支
def test_chat_text_area_history_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        widgets_input,
        "_save_input_history_entry",
        lambda _text, **_kwargs: None,
    )
    area = ChatTextArea()
    area.set_history(["旧输入", "新输入"])

    area.text = "草稿"
    area._history_up()

    assert area.text == "新输入"

    area._history_up()

    assert area.text == "旧输入"

    area._history_down()
    area._history_down()

    assert area.text == "草稿"
    assert area._history_index is None


# 功能：验证 record_history 连续去重并限制历史上限
# 设计：替换持久化函数避免落盘，重复与超量录入后检查内存列表与去重行为
def test_chat_text_area_record_history_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        widgets_input,
        "_save_input_history_entry",
        lambda _text, **_kwargs: None,
    )
    area = ChatTextArea()

    area.record_history("alpha")
    area.record_history("alpha")
    area.record_history("beta")

    assert area._history == ["alpha", "beta"]

    area.set_history([f"msg-{i}" for i in range(500)])
    area.record_history("new-entry")

    assert len(area._history) == 500
    assert area._history[-1] == "new-entry"


# 功能：验证输入框关闭历史或识别出密钥时既不写磁盘也不进入上下键回溯
# 设计：替换持久化边界并依次提交普通值、API key 和关闭后的值，检查内存与写入调用一致
def test_chat_text_area_history_opt_out_and_secret_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persisted: list[str] = []
    monkeypatch.setattr(
        widgets_input,
        "_save_input_history_entry",
        lambda text, **_kwargs: persisted.append(text),
    )
    area = ChatTextArea()

    area.record_history("普通任务")
    area.record_history("api_key=sk-secret-secret-123456")
    area.set_history_enabled(False)
    area.record_history("关闭后的任务")

    assert area._history == ["普通任务"]
    assert persisted == ["普通任务"]


# 功能：验证 ChatTextArea 将历史写入实例绑定的工作区路径
# 设计：为两个输入框指定不同临时文件后分别提交，断言物理记录不串扰
def test_chat_text_area_record_history_uses_instance_workspace_path(tmp_path: Path) -> None:
    first_path = tmp_path / "first" / "history.jsonl"
    second_path = tmp_path / "second" / "history.jsonl"
    first = ChatTextArea()
    second = ChatTextArea()
    first.set_history([], path=first_path)
    second.set_history([], path=second_path)

    first.record_history("first task")
    second.record_history("second task")

    assert _load_input_history(path=first_path) == ["first task"]
    assert _load_input_history(path=second_path) == ["second task"]


# 功能：验证自动会话标题从首条用户消息派生且跳过斜杠命令
# 设计：纯函数测试长文本截断、空白折叠与 / 开头输入不产生标题三种边界
def test_derive_session_title_edges() -> None:
    assert _derive_session_title("  帮我修复   登录问题  ") == "帮我修复 登录问题"
    assert _derive_session_title("一句话" * 20) == "一句话" * 6 + "一句…"
    assert _derive_session_title("/model gpt-4") == ""
    assert _derive_session_title("   ") == ""


# 功能：验证会话选择器按标题/ID/状态子串过滤且不区分大小写
# 设计：构造三条会话数据直接调用过滤方法，覆盖标题命中与 ID 命中两类查询
def test_session_picker_filters_by_title_and_id() -> None:
    sessions = [
        {"session_id": "s-aaa", "title": "修复登录", "status": "open"},
        {"session_id": "s-bbb", "title": "Untitled", "status": "open"},
        {"session_id": "s-ccc", "title": "重构模块", "status": "closed"},
    ]
    picker = SessionPicker(sessions, "s-aaa")

    picker._filter = "登录"
    assert [s["session_id"] for s in picker._filtered_sessions()] == ["s-aaa"]

    picker._filter = "S-BB"
    assert [s["session_id"] for s in picker._filtered_sessions()] == ["s-bbb"]

    picker._filter = "closed"
    assert [s["session_id"] for s in picker._filtered_sessions()] == ["s-ccc"]

    picker._filter = "不存在"
    assert picker._filtered_sessions() == []

    picker._filter = ""
    assert len(picker._filtered_sessions()) == 3


# 功能：验证会话选择器渲染包含过滤命中提示与空结果引导
# 设计：渲染 Rich markup 后检查纯文本，避免样式标签干扰断言
def test_session_picker_render_shows_filter_summary() -> None:
    sessions = [
        {"session_id": "s-1", "title": "修复登录", "status": "open"},
        {"session_id": "s-2", "title": "重构模块", "status": "open"},
    ]
    picker = SessionPicker(sessions, None)
    picker._filter = "登录"

    plain = render(picker._render_ui()).plain

    assert "过滤：登录" in plain
    assert "命中 1/2" in plain
    assert "修复登录" in plain
    assert "重构模块" not in plain

    picker._filter = "zzz"
    plain = render(picker._render_ui()).plain

    assert "没有匹配的会话" in plain


# 功能：验证 /help 在日志中渲染键位说明和命令清单
# 设计：挂载真实 TUI 并捕获追加的 Static，检查帮助内容包含新命令与关键键位
async def test_help_command_renders_keys_and_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app_module, "_load_input_history", lambda: [])

    class HelpHarness(CodeRookTuiApp):
        # 跳过 socket 连接与横幅，仅构建命令表并聚焦输入框
        def on_mount(self) -> None:
            self._slash_items = self._build_slash_items()
            self.query_one("#prompt", ChatTextArea).focus()

    app = HelpHarness("127.0.0.1", 9999)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        appended: list[str] = []

        def capture(widget: Any) -> None:
            appended.append(str(widget.render()))

        app._append = capture  # type: ignore[method-assign]
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "/help"

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        await pilot.pause()

        body = "\n".join(appended)
        assert "键位" in body
        assert "Ctrl+End" in body
        assert "命令" in body
        assert "/rename" in body
        assert "/fork" in body
        assert "/export" in body
        assert "/delete" in body


# 功能：验证 Ctrl+C 取消任务需要二次确认，第一次只提示不发送取消
# 设计：挂载 TUI 并替换取消 worker，连续两次触发绑定动作检查提示与实际取消的先后顺序
async def test_cancel_run_requires_second_ctrl_c() -> None:
    cancelled: list[str] = []

    class CancelHarness(CodeRookTuiApp):
        # 跳过 socket，仅保留界面骨架与状态位
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

        # 记录真实的取消请求以便断言第二次确认后才触发
        async def _do_cancel_run(self, run_id: str) -> None:
            cancelled.append(run_id)

    app = CancelHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        app._client = _FakeClient()
        app._busy = True
        app._active_run_id = "run-1"

        await app.action_cancel_run()
        await pilot.pause()

        assert cancelled == []
        assert app._cancel_armed

        await app.action_cancel_run()
        await pilot.pause()

        assert cancelled == ["run-1"]


# 功能：验证 run.started 事件复位取消确认，避免下一个 run 继承武装状态
# 设计：先武装确认位再投递 run.started 事件，检查状态位被清零
async def test_run_started_resets_cancel_arm() -> None:
    class Harness(CodeRookTuiApp):
        # 只挂载界面，不连接 socket
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = Harness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        app._cancel_armed = True

        app._handle_event({"type": "run.started", "run_id": "run-2"})

        assert not app._cancel_armed


# 功能：验证顶栏常驻显示 context 水位条并随 llm.usage 更新
# 设计：挂载 TUI 后先更新占用率再刷新顶栏，检查 header 文本包含 ctx 指示
async def test_header_shows_context_bar() -> None:
    class HeaderHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = HeaderHarness("127.0.0.1", 9999, locale="en-US")
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()

        app._last_context_pct = 0.42
        app._update_header("running")
        await pilot.pause()
        header_text = str(app.query_one("#header").render())

        assert "ctx:42%" in header_text
        assert "running" in header_text

        app._handle_event({"type": "llm.usage", "run_id": "run-x", "context_pct": 0.9})
        await pilot.pause()
        header_text = str(app.query_one("#header").render())

        assert "ctx:90%" in header_text


# 功能：验证 /rename 通过 IPC 调用 session.rename 并更新本地标题状态
# 设计：注入假客户端直接 await worker 协程，断言命令参数与状态同步及提示行输出
async def test_rename_session_updates_title_state() -> None:
    class RenameHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = RenameHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        client = _FakeClient({"session.rename": {"session": {"session_id": "s-1", "title": "新标题"}}})
        app._client = client
        app._session_id = "s-1"
        appended: list[str] = []
        app._append = lambda widget: appended.append(str(widget.render()))  # type: ignore[method-assign]

        await app._do_rename_session("新标题")
        await pilot.pause()

        assert client.calls == [
            ("session.rename", {"session_id": "s-1", "title": "新标题"})
        ]
        assert app._session_title == "新标题"
        assert app._titled
        assert any("会话已重命名" in line for line in appended)


# 功能：验证首个 run 成功后按首条用户消息自动命名会话
# 设计：注入假客户端与未命名状态，触发自动标题逻辑后断言 rename 调用且不产生提示行
async def test_auto_title_after_first_successful_run() -> None:
    class AutoTitleHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = AutoTitleHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        client = _FakeClient({"session.rename": {"session": {"session_id": "s-9", "title": "x"}}})
        app._client = client
        app._session_id = "s-9"
        app._titled = False
        app._first_user_text = "帮我修复登录页的空指针崩溃"
        appended: list[str] = []
        app._append = lambda widget: appended.append(str(widget.render()))  # type: ignore[method-assign]

        app._maybe_autotitle_session()
        await pilot.pause()
        await pilot.pause()

        assert client.calls and client.calls[0][0] == "session.rename"
        assert client.calls[0][1]["title"] == "帮我修复登录页的空指针崩溃"[0:20]
        assert app._titled
        assert appended == []


# 功能：验证 TUI 切换会话时为新 session 建立带游标的 thread 订阅
# 设计：在真实 Textual 骨架中调用 _load_session，截取 IPC 参数验证渲染前已切换订阅边界
async def test_load_session_subscribes_selected_thread() -> None:
    class SessionHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = SessionHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        client = _FakeClient(
            {
                "session.get_history": {"messages": []},
                "event.subscribe": {"subscription_id": "sub-sess", "last_seq": 4},
            }
        )
        app._client = client

        # 跳过本用例不关心的 authority 与 Goal IPC 刷新
        async def no_refresh() -> None:
            return None

        app._refresh_authority = no_refresh  # type: ignore[method-assign]
        app._refresh_goal_state = no_refresh  # type: ignore[method-assign]

        await app._load_session("sess-selected", resume=True, title="Selected")
        await pilot.pause()

        thread_calls = [
            params
            for method, params in client.calls
            if method == "event.subscribe" and "thread_id" in params
        ]
        assert len(thread_calls) == 1
        assert thread_calls[0]["thread_id"] == "sess-selected"
        assert thread_calls[0]["after_seq"] == 0
        assert "llm.reasoning" in thread_calls[0]["topics"]
        assert app._session_id == "sess-selected"
        assert app._connection._session_cursors["sess-selected"] == 4


# 功能：验证手动切换到活动会话时使用只读 thread 附着而不会被 SESSION_BUSY 阻断
# 设计：让 session.resume 返回稳定 busy 错误并由 thread.get 提供权威记录，截取 load 调用证明切换继续进入订阅与恢复路径
async def test_manual_switch_attaches_active_session_after_resume_busy() -> None:
    class _BusyClient:
        # 初始化 IPC 调用记录
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        # 对 resume 返回 busy，并为只读附着返回同一 thread
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, dict(params)))
            if method == "session.resume":
                raise IpcError(-32012, "session busy")
            if method == "thread.get":
                return {
                    "thread": {
                        "id": "sess-active",
                        "title": "Active task",
                        "status": "running",
                    }
                }
            raise AssertionError(method)

    app = CodeRookTuiApp("127.0.0.1", 9999)
    client = _BusyClient()
    app._client = client  # type: ignore[assignment]
    app._connection = TuiConnection(app, None, host="127.0.0.1", port=9999)
    app._session_id = "sess-current"
    loaded: list[tuple[str, bool, str | None]] = []

    # 截取实际视图加载参数，隔离本测试不关心的 Textual 挂载细节
    async def capture_load(
        session_id: str,
        *,
        resume: bool,
        title: str | None = None,
    ) -> None:
        loaded.append((session_id, resume, title))

    app._load_session = capture_load  # type: ignore[method-assign]

    await app._switch_session("sess-active")

    assert client.calls == [
        ("session.resume", {"session_id": "sess-active"}),
        ("thread.get", {"thread_id": "sess-active"}),
    ]
    assert loaded == [("sess-active", True, "Active task")]


# 功能：验证 /delete 未带 --yes 时只显示确认提示，不发送删除命令
# 设计：挂载 TUI 捕获日志，直接调用提交事件断言没有 IPC 调用且有二次确认引导
async def test_delete_requires_explicit_confirmation() -> None:
    class DeleteHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = DeleteHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        client = _FakeClient()
        app._client = client
        app._session_id = "s-del"
        appended: list[str] = []
        app._append = lambda widget: appended.append(str(widget.render()))  # type: ignore[method-assign]
        prompt = app.query_one("#prompt", ChatTextArea)
        prompt.text = "/delete"

        await app.on_chat_text_area_submitted(ChatTextArea.Submitted(prompt))
        await pilot.pause()

        assert client.calls == []
        assert any("/delete --yes" in line for line in appended)


# 功能：验证 /export 把会话内容写入工作区文件并展示路径
# 设计：把 Path.cwd 重定向到临时目录，注入假客户端返回 markdown 内容后检查文件落盘
async def test_export_session_writes_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)

    class ExportHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = ExportHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        client = _FakeClient(
            {"session.export": {"filename": "export-test.md", "content": "# 会话内容"}}
        )
        app._client = client
        app._session_id = "s-exp"
        appended: list[str] = []
        app._append = lambda widget: appended.append(str(widget.render()))  # type: ignore[method-assign]

        await app._do_export_session("md")
        await pilot.pause()

        assert client.calls == [
            ("session.export", {"session_id": "s-exp", "format": "markdown"})
        ]
        assert (tmp_path / "export-test.md").read_text(encoding="utf-8") == "# 会话内容"
        assert any("会话已导出" in line for line in appended)


# 功能：验证会话导出默认拒绝覆盖，仅显式二次确认才替换旧文件
# 设计：预先写入同名文件，先断言普通导出保持原内容，再以 overwrite 边界确认改写
async def test_export_session_refuses_overwrite_without_explicit_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    target = tmp_path / "export-test.md"
    target.write_text("old content", encoding="utf-8")
    client = _FakeClient(
        {"session.export": {"filename": target.name, "content": "new content"}}
    )
    app = CodeRookTuiApp("127.0.0.1", 9999)
    app._client = client
    app._session_id = "s-exp"
    appended: list[str] = []
    app._append = lambda widget: appended.append(str(widget.render()))  # type: ignore[method-assign]
    app._restore_ready_prompt = lambda: None  # type: ignore[method-assign]

    await app._do_export_session("md")

    assert target.read_text(encoding="utf-8") == "old content"
    assert any(str(target) in line and "--force --yes" in line for line in appended)

    await app._do_export_session("md", overwrite=True)
    assert target.read_text(encoding="utf-8") == "new content"


# 功能：验证用户上滚离开底部后新内容不再强制拉回底部
# 设计：先在底部追加确认跟随，再滚动到顶部追加新行，断言滚动位置保持不变
async def test_append_pauses_autoscroll_when_user_scrolled_up() -> None:
    class ScrollHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = ScrollHarness("127.0.0.1", 9999)
    async with app.run_test(size=(60, 10)) as pilot:
        await pilot.pause()
        log_view = app.query_one("#log-view", VerticalScroll)

        for i in range(30):
            app._append(Static(f"line-{i}"))
        await pilot.pause()
        await pilot.pause()

        log_view.scroll_end(animate=False)
        await pilot.pause()
        await pilot.pause()

        assert log_view.is_vertical_scroll_end

        log_view.scroll_to(y=0, animate=False)
        await pilot.pause()
        await pilot.pause()

        app._append(Static("new-content-after-scroll"))
        await pilot.pause()
        await pilot.pause()

        assert log_view.scroll_offset.y == 0

        app.action_scroll_log_end()
        await pilot.pause()
        await pilot.pause()

        assert log_view.is_vertical_scroll_end


# 功能：验证审批超时/断连事件输出明确提示而不是静默移除卡片
# 设计：挂载 TUI 后先投递 permission.requested 再投递 denied，检查日志包含拒绝说明
async def test_permission_denied_shows_notice() -> None:
    class DenyHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = DenyHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        appended: list[str] = []
        app._append = lambda widget: appended.append(str(widget.render()))  # type: ignore[method-assign]

        app._handle_event(
            {
                "type": "permission.requested",
                "tool_use_id": "tu-1",
                "tool_name": "bash",
                "param_preview": "command='rm -rf /tmp/x'",
                "params": {},
            }
        )
        await pilot.pause()

        app._handle_event({"type": "permission.denied", "tool_use_id": "tu-1"})
        await pilot.pause()

        body = "\n".join(appended)
        assert "审批超时或连接断开" in body
        assert "bash" in body

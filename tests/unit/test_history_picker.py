from unittest.mock import AsyncMock, MagicMock

from textual.app import App

from code_rook.tui.widgets.history import HistoryPicker


# 功能：导航恢复原会话并回填用户文本，不提交新任务。
# 设计：执行生产 App 操作方法，检查 IPC 方法、加载目标、输入框及草稿保存调用。
async def test_navigation_restores_editor_without_sending() -> None:
    from code_rook.tui.app import CodeRookTuiApp

    app = MagicMock()
    app._session_id = "sess-1"
    app._session_title = "Original"
    app._client.send_command = AsyncMock(return_value={"editor_text": "Revise this question"})
    app._load_session = AsyncMock()
    await CodeRookTuiApp._do_navigate_session(app, 7)
    app._client.send_command.assert_awaited_once_with(
        "session.navigate", {"session_id": "sess-1", "target_seq": 7, "summarize": False},
    )
    app._load_session.assert_awaited_once_with("sess-1", resume=True, title="Original")
    assert app._prompt().text == "Revise this question"
    app._snapshot_session_composer.assert_called_once()
    assert app._navigation_inflight is False


# 功能：历史选择器可直接通过键盘选择 Ledger 节点。
# 设计：运行真实 Textual 测试界面并按 Enter，验证选择结果不依赖斜杠参数解析。
async def test_history_picker_selects_entry() -> None:
    selected: list[tuple[str, int] | None] = []
    app: App[None] = App()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(
            HistoryPicker([{"seq": 7, "active": True, "preview": "Fix login"}]),
            selected.append,
        )
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert selected == [("navigate", 7)]


# 功能：显式分支快捷键与原地导航返回不同动作。
# 设计：运行相同历史列表并使用 Ctrl+F，避免默认 Enter 意外创建新会话。
async def test_history_picker_forks_entry() -> None:
    selected: list[tuple[str, int] | None] = []
    app: App[None] = App()
    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(
            HistoryPicker([{"seq": 7, "active": True, "preview": "Fix login"}]),
            selected.append,
        )
        await pilot.pause()
        await pilot.press("ctrl+f")
        await pilot.pause()
    assert selected == [("fork", 7)]

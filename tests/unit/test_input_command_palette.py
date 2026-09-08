from unittest.mock import AsyncMock, MagicMock

from textual.app import App

from code_rook.tui.app import CodeRookTuiApp
from code_rook.tui.commands import match_slash_command
from code_rook.tui.widgets.palette import CommandPalette, CommandPaletteItem


# 功能：重新加载命令更新模板和 Skills 补全，不创建模型任务。
# 设计：调用注册表中的真实处理器并核对唯一 IPC 请求，确保 reload 不是发给模型的普通文本。
async def test_reload_refreshes_catalog_without_model_call() -> None:
    app = MagicMock()
    app._session_id = "sess-reload"
    app._locale = "en-US"
    app._client.send_command = AsyncMock(return_value={"input_commands": [
        {"name": "new-template", "kind": "template", "description": "New"},
    ]})
    area = MagicMock()
    area.text = "/reload"
    command = match_slash_command("/reload")
    assert command is not None
    await command.handler(app, area, "/reload")
    assert app._input_commands[0]["name"] == "new-template"
    assert area.text == ""
    app._client.send_command.assert_awaited_once_with("session.reload", {"session_id": "sess-reload"})
    app.notify.assert_called_once()


# 功能：验证 Ctrl+P 包含原生命令且选择模板只填入输入框，不自动提交任务。
# 设计：调用生产面板构建和选择处理器，检查 direct 标记、保留命令优先与发送调用次数。
async def test_template_palette_selection_only_fills_composer() -> None:
    app = MagicMock()
    app._locale = "en-US"
    app._labs_enabled = False
    app._input_commands = [
        {"name": "brief", "description": "Summarize current work", "kind": "template",
         "argument_hint": "<path>"},
        {"name": "skill:review", "description": "Review code", "kind": "skill"},
        {"name": "help", "description": "Must not shadow help", "kind": "template"},
    ]
    app._command_usage.side_effect = lambda _name, usage: usage
    items = CodeRookTuiApp._build_palette_items(app)
    assert sum(item.command == "help" for item in items) == 1
    template = next(item for item in items if item.command == "brief")
    assert not template.direct
    assert template.usage == "<path>"
    assert any(item.command == "skill:review" for item in items)
    app._client.send_command = AsyncMock()
    await CodeRookTuiApp.on_command_palette_selected(
        app, CommandPalette.Selected(MagicMock(), template)
    )
    assert app._prompt().text == "/brief "
    app._client.send_command.assert_not_called()


# 功能：验证会话已切换时旧目录请求不能覆盖当前命令候选。
# 设计：让旧会话异步请求在新会话 ID 下返回，检查候选保持原值。
async def test_old_session_command_catalog_is_ignored() -> None:
    app = MagicMock()
    app._session_id = "new"
    app._input_commands = [{"name": "new-template"}]
    app._client.send_command = AsyncMock(return_value={
        "input_commands": [{"name": "old-template"}],
    })
    await CodeRookTuiApp._refresh_input_commands(app, "old")
    assert app._input_commands == [{"name": "new-template"}]


# 功能：验证真实 Textual 面板可搜索并用 Enter 选择模板候选。
# 设计：通过键盘事件而非直接调用选择函数覆盖过滤、焦点及消息投递。
async def test_palette_keyboard_selects_template() -> None:
    selected: list[str] = []

    class PaletteApp(App[None]):
        # 收集面板发送的真实选择事件。
        def on_command_palette_selected(self, event: CommandPalette.Selected) -> None:
            selected.append(event.item.command)

    app = PaletteApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await app.mount(CommandPalette([
            CommandPaletteItem("help", "Help", "task", direct=True),
            CommandPaletteItem("brief", "Summarize", "extension"),
        ]))
        await pilot.pause()
        await pilot.press("b", "r", "i", "enter")
        await pilot.pause()
    assert selected == ["brief"]

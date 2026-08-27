from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult

from code_rook.tui import app as tui_app_module
from code_rook.tui.app import CodeRookTuiApp
from code_rook.tui.panels import ChangeCenterOverlay
from code_rook.tui.widgets.input import ChatTextArea
from code_rook.tui.widgets.palette import CommandPalette


# 构造同时包含文件、hunk、冲突安全状态和 digest 的工作区 diff
def _workspace_diff() -> dict[str, object]:
    return {
        "payload": {
            "scope": "all",
            "files": [
                {
                    "path": "src/a.py",
                    "index_status": " ",
                    "worktree_status": "M",
                    "additions": 2,
                    "deletions": 1,
                },
                {
                    "path": "tests/test_a.py",
                    "index_status": " ",
                    "worktree_status": "M",
                    "additions": 1,
                    "deletions": 0,
                },
            ],
            "additions": 3,
            "deletions": 1,
            "diff": (
                "diff --git a/src/a.py b/src/a.py\n"
                "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n-old\n+new\n+line\n"
                "diff --git a/tests/test_a.py b/tests/test_a.py\n"
                "--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@ -1 +1 @@\n-old\n+new"
            ),
            "state_digest": "a" * 64,
        }
    }


# 构造最近 Turn 的 durable 验证映射
def _receipt() -> dict[str, object]:
    return {
        "receipt": {
            "finished_at": "2026-08-24T10:00:00Z",
            "verification": [
                {
                    "action": "tests",
                    "paths": ["src/a.py", "tests/test_a.py"],
                    "gates": [
                        {
                            "name": "pytest",
                            "command": "pytest -q",
                            "status": "passed",
                        }
                    ],
                }
            ],
            "unavailable": [],
        }
    }


class _ShellHarness(CodeRookTuiApp):
    # 初始化不启动 socket 的产品壳测试 App
    def __init__(self, *, locale: str = "zh-CN") -> None:
        super().__init__("127.0.0.1", 9999, locale=locale)

    # 只激活 composer、命令元数据和顶栏，避免单元测试连接真实 Core
    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = False
        self._update_header("ready")


@pytest.mark.parametrize("size", [(80, 24), (100, 30), (140, 40)])
# 功能：验证 Change Center 在三种目标终端尺寸均可聚焦导航并用 Esc 关闭
# 设计：用真实 Textual App 挂载 overlay，驱动 j/n/Esc 后断言文件、hunk、验证映射和关闭消息
async def test_change_center_overlay_navigation_across_sizes(
    size: tuple[int, int],
) -> None:
    dismissed: list[bool] = []

    class _OverlayHarness(App[None]):
        # 挂载带 durable receipt 的真实 Change Center overlay
        def compose(self) -> ComposeResult:
            yield ChangeCenterOverlay(_workspace_diff(), _receipt(), locale="en-US")

        # 记录 Esc 关闭请求
        def on_change_center_overlay_dismissed(
            self,
            _message: ChangeCenterOverlay.Dismissed,
        ) -> None:
            dismissed.append(True)

    app = _OverlayHarness()
    async with app.run_test(size=size) as pilot:
        overlay = app.query_one(ChangeCenterOverlay)
        await pilot.pause()
        assert overlay.has_focus
        assert "pytest -q" in str(overlay.render())
        await pilot.press("j", "n")
        assert overlay.panel.current_file is not None
        assert overlay.panel.current_file.path == "tests/test_a.py"
        await pilot.press("escape")
        await pilot.pause()

    assert dismissed == [True]


# 功能：验证 Ctrl+P 候选按七类组织、常用命令置顶且 Labs 默认隐藏
# 设计：读取真实 App 构建结果并渲染中英文面板，避免只测试重复维护的静态列表
def test_command_palette_categories_priority_and_labs_boundary() -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999, locale="en-US")
    items = app._build_palette_items()
    names = [item.command for item in items]
    palette = CommandPalette(items, locale="en-US")
    rendered = Text.from_markup(str(palette.render())).plain

    assert names[0] != "workflow"
    assert "workflow" not in names
    assert "hooks" not in names
    assert {"task", "session", "review", "model", "security", "extension"}.issubset(
        {item.category for item in items}
    )
    assert "Change Center" in rendered
    assert "Tasks" in rendered
    assert "Review" in rendered
    model_item = next(item for item in items if item.command == "model")
    assert model_item.usage == "<model ID>|add <model ID>"


@pytest.mark.parametrize("width", [80, 100, 140])
# 功能：验证专注顶栏在三档列宽只保留仓库、模型和权威运行阶段
# 设计：固定内部会话与 route 后检查它们不泄露，产品状态移入独立底栏且 Rich 文本不溢出
def test_responsive_header_field_contract(width: int) -> None:
    app = CodeRookTuiApp("127.0.0.1", 9999, locale="en-US")
    app._session_id = "session-1234"
    app._route = "deepseek"
    app._model = "deepseek-coder"
    app._active_goal_id = "goal-1"
    app._active_goal_status = "running"
    app._cost_total = 0.0123
    markup = app._render_responsive_header("ready", width)
    plain = Text.from_markup(markup)

    assert plain.cell_len <= width
    assert "coderook" in plain.plain.casefold()
    assert "deepseek" in plain.plain
    assert "ready" in plain.plain
    assert "session-1234" not in plain.plain
    assert "route:" not in plain.plain
    assert "trust:" not in plain.plain
    assert "goal:" not in plain.plain


# 功能：验证附件条持久显示序号、尺寸、大小和短 hash，并支持 remove/clear
# 设计：在真实 composer 布局中直接刷新附件状态，检查命令后条带可见性与数组同步
async def test_attachment_strip_and_management_commands() -> None:
    app = _ShellHarness(locale="en-US")
    async with app.run_test(size=(100, 30)) as pilot:
        app._pending_image_attachments = [
            {
                "sha256": "abcdef1234567890",
                "media_type": "image/png",
                "size": 1536,
                "width": 640,
                "height": 480,
            }
        ]
        app._refresh_attachment_strip()
        await pilot.pause()
        strip = app.query_one("#attachment-strip")
        plain = Text.from_markup(str(strip.render())).plain
        assert "#1" in plain and "640x480" in plain
        assert "1.5 KiB" in plain and "abcdef123456" in plain

        app._handle_attachments_command("remove 1")
        await pilot.pause()
        assert app._pending_image_attachments == []
        assert strip.styles.display == "none"

        app._pending_image_attachments = [
            {"sha256": "1" * 64, "size": 10, "width": 1, "height": 1}
        ]
        app._handle_attachments_command("clear")
        assert app._pending_image_attachments == []


# 功能：验证 /language 切换后顶栏、composer、命令面板和附件条立即改用英文
# 设计：先以中文挂载全部持久壳组件，再替换 locale 持久化并切换，断言无需重启即可刷新
async def test_language_switch_refreshes_existing_product_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app_module, "save_locale", lambda _value: "en-US")
    app = _ShellHarness(locale="zh-CN")
    async with app.run_test(size=(140, 40)) as pilot:
        app._client = object()  # type: ignore[assignment]
        app._header_state = "ready"
        app._pending_image_attachments = [
            {"sha256": "f" * 64, "size": 8, "width": 16, "height": 16}
        ]
        app._refresh_attachment_strip()
        app.action_command_palette()
        await pilot.pause()

        app._handle_language_command("en-US")
        await pilot.pause()

        header = Text.from_markup(str(app.query_one("#header").render())).plain
        prompt = app.query_one("#prompt", ChatTextArea)
        palette = app.query_one(CommandPalette)
        strip = Text.from_markup(str(app.query_one("#attachment-strip").render())).plain
        assert "CodeRook" in header and "ready" in header
        assert prompt.border_title == "Describe a task or press Ctrl+P"
        assert "Command Palette" in str(palette.render())
        assert "Pending attachments" in strip

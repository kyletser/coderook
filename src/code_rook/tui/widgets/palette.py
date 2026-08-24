"""Ctrl+P 分类命令面板。"""

from __future__ import annotations

from dataclasses import dataclass

from rich.markup import escape
from textual import events
from textual.message import Message
from textual.widgets import Static

from code_rook.tui.product import tr


@dataclass(frozen=True)
class CommandPaletteItem:
    command: str
    description: str
    category: str
    usage: str = ""
    direct: bool = False
    priority: int = 100


class CommandPalette(Static):
    """可搜索、可聚焦且按产品类别分组的命令入口。"""

    can_focus = True

    DEFAULT_CSS = """
    CommandPalette {
        layer: overlay;
        width: 90%;
        max-width: 100;
        height: 80%;
        max-height: 32;
        align: center middle;
        margin: 2 4;
        padding: 1 2;
        border: round #4d8994;
        background: #17191d;
        color: $text;
    }
    """

    class Selected(Message):
        # 初始化命令面板选择消息
        def __init__(self, palette: CommandPalette, item: CommandPaletteItem) -> None:
            self.palette = palette
            self.item = item
            super().__init__()

    class Dismissed(Message):
        # 初始化命令面板关闭消息
        def __init__(self, palette: CommandPalette) -> None:
            self.palette = palette
            super().__init__()

    # 初始化分类命令、语言、过滤条件与光标
    def __init__(self, items: list[CommandPaletteItem], *, locale: str = "zh-CN") -> None:
        super().__init__(classes="command-palette")
        self._items = sorted(items, key=lambda item: (item.priority, item.category, item.command))
        self._locale = locale
        self._query = ""
        self._cursor = 0
        self.update(self._render_ui())

    # 挂载后接管键盘焦点
    def on_mount(self) -> None:
        self.focus()

    # 原地切换语言并刷新当前筛选视图
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        self.update(self._render_ui())

    # 返回按命令名和说明过滤后的稳定候选
    def _filtered_items(self) -> list[CommandPaletteItem]:
        query = self._query.casefold().strip()
        if not query:
            return list(self._items)
        return [
            item
            for item in self._items
            if query in item.command.casefold() or query in item.description.casefold()
        ]

    # 渲染搜索词、分类标题、候选项和键盘提示
    def _render_ui(self) -> str:
        items = self._filtered_items()
        if items:
            self._cursor = min(self._cursor, len(items) - 1)
        else:
            self._cursor = 0
        query = self._query or tr("palette.search.empty", self._locale)
        lines = [
            f"[bold cyan]{escape(tr('palette.title', self._locale))}[/bold cyan]  "
            f"[dim]{escape(tr('palette.search', self._locale, query=query))}[/dim]"
        ]
        if not items:
            lines.append(f"[yellow]{escape(tr('palette.no_results', self._locale))}[/yellow]")
        previous_category = ""
        for index, item in enumerate(items[:18]):
            if item.category != previous_category:
                previous_category = item.category
                lines.append(
                    f"[bold]{escape(tr(f'palette.category.{item.category}', self._locale))}[/bold]"
                )
            marker = "[cyan]›[/cyan]" if index == self._cursor else " "
            usage = f" {item.usage}" if item.usage else ""
            lines.append(
                f"{marker} [bold]/{escape(item.command)}{escape(usage)}[/bold]  "
                f"[dim]{escape(item.description)}[/dim]"
            )
        if len(items) > 18:
            lines.append(
                f"[dim]{escape(tr('palette.more', self._locale, count=len(items) - 18))}[/dim]"
            )
        lines.append(f"[dim]{escape(tr('palette.hint', self._locale))}[/dim]")
        return "\n".join(lines)

    # 处理搜索输入、上下移动、选择和 Esc 关闭
    def on_key(self, event: events.Key) -> None:
        items = self._filtered_items()
        if event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))
            return
        if event.key in {"up", "ctrl+p"} and items:
            event.stop()
            self._cursor = (self._cursor - 1) % len(items)
        elif event.key in {"down", "ctrl+n"} and items:
            event.stop()
            self._cursor = (self._cursor + 1) % len(items)
        elif event.key == "enter" and items:
            event.stop()
            self.post_message(self.Selected(self, items[self._cursor]))
            return
        elif event.key == "backspace":
            event.stop()
            self._query = self._query[:-1]
            self._cursor = 0
        elif (
            event.character
            and len(event.character) == 1
            and event.is_printable
            and event.key not in {"up", "down", "enter", "escape", "tab"}
        ):
            event.stop()
            self._query += event.character
            self._cursor = 0
        else:
            return
        self.update(self._render_ui())

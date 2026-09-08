from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option


class HistoryPicker(ModalScreen[tuple[str, int] | None]):
    BINDINGS = [
        ("escape", "dismiss_picker", "Close"), ("ctrl+f", "fork_entry", "Fork"),
        ("ctrl+s", "summarize_entry", "Summarize and navigate"),
    ]
    DEFAULT_CSS = """
    HistoryPicker { align: center middle; }
    HistoryPicker > OptionList { width: 85%; height: 70%; border: round $primary; }
    HistoryPicker > Label { width: 85%; padding: 1; background: $surface; }
    """

    # 保存历史节点，只展示有可读消息预览的条目。
    def __init__(self, entries: list[dict[str, Any]], locale: str = "zh-CN") -> None:
        super().__init__()
        self._entries = [entry for entry in entries if entry.get("preview")]
        self._locale = locale

    # 构建键盘可选历史列表，条目按原日志顺序保留分支标识。
    def compose(self) -> ComposeResult:
        zh = self._locale == "zh-CN"
        yield Label(
            "Enter 切换 · Ctrl+S 带摘要切换（调用模型） · Ctrl+F 分支 · Esc 取消"
            if zh else "Enter navigate · Ctrl+S summarize (model call) · Ctrl+F fork · Esc close"
        )
        yield OptionList(*[
            Option(
                Text(f"{'●' if entry['active'] else '○'} #{entry['seq']}  {entry['preview']}"),
                id=str(entry["seq"]),
            )
            for entry in self._entries
        ])

    # 提交所选 Ledger 节点，在同一会话内导航。
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(("navigate", int(event.option.id)))

    # 显式请求模型总结离开的分支后再导航，不改变默认操作的零调用行为。
    def action_summarize_entry(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            option = options.get_option_at_index(options.highlighted)
            if option.id is not None:
                self.dismiss(("summarize", int(option.id)))

    # 把当前高亮节点作为独立分支，不改变默认 Enter 的导航行为。
    def action_fork_entry(self) -> None:
        options = self.query_one(OptionList)
        if options.highlighted is not None:
            option = options.get_option_at_index(options.highlighted)
            if option.id is not None:
                self.dismiss(("fork", int(option.id)))

    # 退出选择器且不改变当前会话。
    def action_dismiss_picker(self) -> None:
        self.dismiss(None)

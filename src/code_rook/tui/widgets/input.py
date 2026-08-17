"""输入区相关控件：API Key 提示、斜杠命令补全与多行聊天输入框。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input, Label, Static, TextArea

from code_rook.core.llm.provider_presets import ProviderPreset

_INPUT_HISTORY_LIMIT = 500


# 返回用户级输入历史文件路径
def _input_history_path() -> Path:
    return Path.home() / ".coderook" / "tui-history.jsonl"


# 从磁盘加载最近的输入历史，坏行静默跳过
def _load_input_history(limit: int = _INPUT_HISTORY_LIMIT) -> list[str]:
    try:
        lines = _input_history_path().read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    history: list[str] = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            text = str(item.get("text", ""))
            if text:
                history.append(text)
    return history


# 将一条输入追加到历史文件，写入失败时静默跳过
def _save_input_history_entry(text: str) -> None:
    if not text.strip():
        return
    path = _input_history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
    except OSError:
        pass


class ConfigApiKeyPrompt(Static):
    """Password input used by the inline provider configuration flow."""

    can_focus = False

    DEFAULT_CSS = """
    ConfigApiKeyPrompt {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
    }
    ConfigApiKeyPrompt Input {
        margin-top: 1;
        border: round $surface-lighten-2;
    }
    ConfigApiKeyPrompt .config-error { color: red; height: auto; }
    """

    class Submitted(Message):
        # 初始化 API Key 提交消息
        def __init__(self, prompt: ConfigApiKeyPrompt, api_key: str) -> None:
            self.prompt = prompt
            self.api_key = api_key
            super().__init__()

    class Dismissed(Message):
        # 初始化 API Key 输入框关闭消息
        def __init__(self, prompt: ConfigApiKeyPrompt) -> None:
            self.prompt = prompt
            super().__init__()

    # 初始化指定 Provider 的密钥输入面板
    def __init__(self, provider: ProviderPreset) -> None:
        super().__init__()
        self.provider = provider

    # 组合说明、密码输入框和错误提示
    def compose(self) -> ComposeResult:
        yield Label(
            f"[bold]{escape(self.provider.name)}[/bold]\n"
            "[dim]输入 API Key 后按 Enter，CodeRook 将探测该账号的可用模型。[/dim]"
        )
        yield Input(placeholder="API Key", password=True, id="config-api-key")
        yield Label("", classes="config-error", id="config-key-error")

    # 挂载时设置步骤提示并聚焦密码输入框
    def on_mount(self) -> None:
        self.border_title = " API Key "
        self.border_subtitle = " Enter discover models   Esc back "
        self.query_one("#config-api-key", Input).focus()

    # 校验密钥非空后发布提交消息
    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        api_key = event.value.strip()
        if not api_key:
            self.show_error("API Key 不能为空。")
            return
        event.input.disabled = True
        self.border_subtitle = " discovering available models... "
        self.post_message(self.Submitted(self, api_key))

    # 显示探测错误并允许用户重新输入
    def show_error(self, message: str) -> None:
        self.query_one("#config-key-error", Label).update(escape(message))
        key_input = self.query_one("#config-api-key", Input)
        key_input.disabled = False
        key_input.focus()
        self.border_subtitle = " Enter retry   Esc back "

    # 捕获 Esc 并返回 Provider 选择页
    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))


@dataclass(frozen=True)
class CompletionItem:
    """一条补全项：命令/skill 名称、说明与可选 usage，供补全弹窗展示与筛选。"""

    name: str
    description: str
    usage: str = ""


# 判断 query 是否按序出现在 text 中（子序列匹配）
def _is_subsequence(q: str, text: str) -> bool:
    if not q:
        return True
    pos = 0
    for ch in text:
        if q[pos] == ch:
            pos += 1
            if pos == len(q):
                return True
    return False


# 对多个字段做大小写不敏感模糊匹配，返回命中字段下标：包含优先于子序列，同级按下标保持稳定
def _fuzzy_match(q: str, *fields: str) -> list[int]:
    if not q:
        return list(range(len(fields)))
    lower_q = q.lower()
    scored: list[tuple[int, int]] = []
    for i, field in enumerate(fields):
        lower_field = field.lower()
        if lower_q in lower_field:
            scored.append((0, i))
        elif _is_subsequence(lower_q, lower_field):
            scored.append((1, i))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [i for _, i in scored]


class SlashCompleteWidget(Static):
    """斜杠命令自动补全弹出框：输入 / 时显示可用 skill 列表并支持键盘筛选与选择。"""

    can_focus = False

    DEFAULT_CSS = """
    SlashCompleteWidget {
        height: auto;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    # 用户选中某条命令时发布
    class Selected(Message):
        # 初始化，携带被选中的 skill 名称
        def __init__(self, skill_name: str) -> None:
            self.skill_name = skill_name
            super().__init__()

    # 初始化，接收全量 CompletionItem 列表
    def __init__(self, items: list[CompletionItem]) -> None:
        super().__init__("")
        self._all_items = list(items)
        self._filtered: list[CompletionItem] = list(items)
        self._cursor = 0

    # 根据查询字符串对 name 与 description 做模糊匹配筛选，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = (query or "").lower()
        if not q:
            self._filtered = list(self._all_items)
        else:
            scored: list[tuple[int, int, CompletionItem]] = []
            for order, item in enumerate(self._all_items):
                # name 命中权重最高，仅有 description 命中时按 description 参与排序
                if _fuzzy_match(q, item.name):
                    scored.append((0, order, item))
                elif _fuzzy_match(q, item.description):
                    scored.append((1, order, item))
            scored.sort(key=lambda x: (x[0], x[1]))
            self._filtered = [item for _, _, item in scored]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上移动光标并重新渲染
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下移动光标并重新渲染
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 选中当前光标项并发布 Selected 消息
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor].name))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    # 判断查询是否已经完整匹配一条命令，供 Enter 直接执行
    def has_exact_match(self, query: str) -> bool:
        return any(item.name == query for item in self._filtered)

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表并高亮当前光标项，底部固定一行显示当前选中项的 usage
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, item in enumerate(self._filtered):
            desc_part = f"  [dim]{item.description}[/dim]" if item.description else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{item.name}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{item.name}[/cyan]{desc_part}")
        selected = self._filtered[self._cursor]
        # 无 usage 时回退显示该条说明，保证底部信息始终有内容
        if selected.usage:
            usage_part = f"usage: /{selected.name} {selected.usage}"
        else:
            usage_part = selected.description
        lines.append(f"[dim]{usage_part}[/dim]")
        lines.append("[dim]  ↑↓ navigate   tab complete   enter run/complete   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round #343b45;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round #596472;
        background: $background;
    }
    ChatTextArea:disabled {
        border: round #2c323a;
        background: $background;
    }
    """

    # 子类自定义的提交消息，供宿主 App 监听
    class Submitted(Message):
        def __init__(self, area: ChatTextArea) -> None:
            self.text_area = area
            self.value = area.text
            super().__init__()

    # 输入内容以 / 开头且无空格时发布，query 为 / 之后的字符串（可为空串）；None 表示收起弹窗
    class SlashChanged(Message):
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    class CycleMode(Message):
        pass

    # 初始化输入历史状态，支持空输入时 ↑/↓ 回溯最近提交
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft: str = ""

    # 设置可回溯的输入历史列表并重置回溯状态
    def set_history(self, history: list[str]) -> None:
        self._history = list(history)
        self._history_index = None
        self._history_draft = ""

    # 记录一条提交输入：连续去重后写入用户级历史文件
    def record_history(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned or (self._history and self._history[-1] == cleaned):
            return
        self._history.append(cleaned)
        if len(self._history) > _INPUT_HISTORY_LIMIT:
            self._history = self._history[-_INPUT_HISTORY_LIMIT:]
        self._history_index = None
        self._history_draft = ""
        _save_input_history_entry(cleaned)

    # 回溯到更早的历史输入；首次进入回溯时保存当前草稿
    def _history_up(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._history_index = len(self._history) - 1
            self._history_draft = self.text
        elif self._history_index > 0:
            self._history_index -= 1
        else:
            return
        self.text = self._history[self._history_index]
        self.move_cursor(self.document.end)

    # 前进到更新的历史输入，越过最新一条时恢复草稿并退出回溯
    def _history_down(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.text = self._history[self._history_index]
        else:
            self._history_index = None
            self.text = self._history_draft
        self.move_cursor(self.document.end)

    # 文本变化时检测 / 前缀，通知宿主 App 更新自动补全弹窗
    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        text = self.text
        if text.startswith("/") and " " not in text:
            self.post_message(ChatTextArea.SlashChanged(query=text[1:]))
        else:
            self.post_message(ChatTextArea.SlashChanged(query=None))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            query = self.text[1:] if self.text.startswith("/") else ""
            if (
                popup is not None
                and popup.has_selection()
                and not popup.has_exact_match(query)
            ):
                popup.select_current()
                return
            if self.text.strip():
                self.post_message(self.Submitted(self))
            return
        if key in ("alt+enter", "shift+enter", "ctrl+j", "super+enter"):
            event.stop()
            event.prevent_default()
            if not self.read_only:
                self.insert("\n")
            return
        if popup is not None:
            if key == "up":
                event.stop()
                event.prevent_default()
                popup.move_up()
                return
            elif key == "down":
                event.stop()
                event.prevent_default()
                popup.move_down()
                return
            elif key == "tab":
                event.stop()
                event.prevent_default()
                popup.select_current()
                return
            elif key == "escape":
                event.stop()
                event.prevent_default()
                self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        if (
            key == "up"
            and popup is None
            and (not self.text or self._history_index is not None)
        ):
            event.stop()
            event.prevent_default()
            self._history_up()
            return
        if key == "down" and popup is None and self._history_index is not None:
            event.stop()
            event.prevent_default()
            self._history_down()
            return
        if key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(ChatTextArea.CycleMode())
            return
        await super()._on_key(event)
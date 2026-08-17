"""键盘驱动的选择器控件：会话、模型与 API Provider。"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import events
from textual.message import Message
from textual.widgets import Static

from code_rook.core.llm.provider_presets import ProviderPreset
from code_rook.tui.widgets import _preview


class SessionPicker(Static):
    """Keyboard-driven picker for saved chat sessions."""

    can_focus = True

    DEFAULT_CSS = """
    SessionPicker {
        height: auto;
        max-height: 16;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    SessionPicker:focus { border: solid #72c7d4; }
    """

    class Selected(Message):
        def __init__(self, picker: SessionPicker, session_id: str) -> None:
            self.picker = picker
            self.session_id = session_id
            super().__init__()

    class Dismissed(Message):
        def __init__(self, picker: SessionPicker) -> None:
            self.picker = picker
            super().__init__()

    def __init__(self, sessions: list[dict[str, Any]], current_session_id: str | None) -> None:
        super().__init__("")
        self._sessions = sessions
        self._current_session_id = current_session_id
        self._filter = ""
        self._cursor = next(
            (
                index
                for index, session in enumerate(sessions)
                if session.get("session_id") == current_session_id
            ),
            0,
        )

    def on_mount(self) -> None:
        self.border_title = " Sessions "
        self.border_subtitle = " 输入即过滤   ↑↓ move   Enter open   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 按过滤串筛选会话，匹配标题、ID 或状态的子串（不区分大小写）
    def _filtered_sessions(self) -> list[dict[str, Any]]:
        query = self._filter.strip().casefold()
        if not query:
            return self._sessions
        return [
            session
            for session in self._sessions
            if query in str(session.get("title", "")).casefold()
            or query in str(session.get("session_id", "")).casefold()
            or query in str(session.get("status", "")).casefold()
        ]

    def _render_ui(self) -> str:
        if not self._sessions:
            return "[dim]没有保存的 chat 会话。[/dim]"
        filtered = self._filtered_sessions()
        self._cursor = min(self._cursor, max(0, len(filtered) - 1))
        lines: list[str] = []
        if self._filter:
            lines.append(
                f"[dim]过滤：{escape(self._filter)}"
                f"  命中 {len(filtered)}/{len(self._sessions)}[/dim]"
            )
        for index, session in enumerate(filtered):
            session_id = escape(str(session.get("session_id", "")))
            title = escape(_preview(str(session.get("title", "")) or "Untitled", 38))
            status = escape(str(session.get("status", "")))
            current = "  [cyan]current[/cyan]" if session_id == self._current_session_id else ""
            if index == self._cursor:
                lines.append(
                    f"[bold #72c7d4]❯[/bold #72c7d4] [bold white]{title}[/bold white]"
                    f"  [#72c7d4]{status}[/#72c7d4]{current}\n"
                    f"  [dim]{session_id}[/dim]"
                )
            else:
                lines.append(
                    f"  [#c6cad0]{title}[/#c6cad0]  [dim]{status}  {session_id}[/dim]{current}"
                )
        if not filtered:
            lines.append("[dim]没有匹配的会话，退格修改过滤词[/dim]")
        return "\n".join(lines)

    def on_key(self, event: events.Key) -> None:
        if event.key == "backspace":
            event.stop()
            self._filter = self._filter[:-1]
            self.update(self._render_ui())
            return
        if (
            event.character
            and len(event.character) == 1
            and event.is_printable
            and event.key not in ("up", "down", "enter", "escape", "tab")
        ):
            event.stop()
            self._filter += event.character
            self.update(self._render_ui())
            return
        filtered = self._filtered_sessions()
        if event.key == "up" and filtered:
            event.stop()
            self._cursor = (self._cursor - 1) % len(filtered)
            self.update(self._render_ui())
        elif event.key == "down" and filtered:
            event.stop()
            self._cursor = (self._cursor + 1) % len(filtered)
            self.update(self._render_ui())
        elif event.key == "enter" and filtered:
            event.stop()
            session_id = str(filtered[self._cursor].get("session_id", ""))
            if session_id:
                self.post_message(self.Selected(self, session_id))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))


class ModelPicker(Static):
    """Keyboard-driven picker for configured models."""

    can_focus = True

    DEFAULT_CSS = """
    ModelPicker {
        height: auto;
        max-height: 16;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    ModelPicker:focus { border: solid #72c7d4; }
    """

    class Selected(Message):
        # 初始化模型选择消息
        def __init__(self, picker: ModelPicker, model: str) -> None:
            self.picker = picker
            self.model = model
            super().__init__()

    class Dismissed(Message):
        # 初始化模型选择器关闭消息
        def __init__(self, picker: ModelPicker) -> None:
            self.picker = picker
            super().__init__()

    # 初始化模型列表并将光标定位到活动模型
    def __init__(self, models: list[str], active_model: str) -> None:
        super().__init__("")
        self._models = models
        self._active_model = active_model
        self._cursor = models.index(active_model) if active_model in models else 0

    # 挂载时设置标题、操作提示和键盘焦点
    def on_mount(self) -> None:
        self.border_title = " Models "
        self.border_subtitle = " ↑↓ move   Enter switch   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 渲染模型列表并标记当前活动模型
    def _render_ui(self) -> str:
        if not self._models:
            return "[dim]No configured models. Use /model add <model-id>.[/dim]"
        window_size = 8
        start = max(0, min(self._cursor - window_size // 2, len(self._models) - window_size))
        end = min(len(self._models), start + window_size)
        lines: list[str] = []
        if start > 0:
            lines.append(f"[dim]  ↑ {start} more[/dim]")
        for index in range(start, end):
            model = self._models[index]
            safe_model = escape(model)
            current = "  [cyan]current[/cyan]" if model == self._active_model else ""
            if index == self._cursor:
                lines.append(
                    f"[bold #72c7d4]❯[/bold #72c7d4] "
                    f"[bold white]{safe_model}[/bold white]{current}"
                )
            else:
                lines.append(f"  [#c6cad0]{safe_model}[/#c6cad0]{current}")
        if end < len(self._models):
            lines.append(f"[dim]  ↓ {len(self._models) - end} more[/dim]")
        lines.append("[dim]Add a custom option with /model add <model-id>[/dim]")
        return "\n".join(lines)

    # 处理上下移动、确认选择和关闭快捷键
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k") and self._models:
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._models)
            self.update(self._render_ui())
        elif event.key in ("down", "j") and self._models:
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._models)
            self.update(self._render_ui())
        elif event.key == "enter" and self._models:
            event.stop()
            self.post_message(self.Selected(self, self._models[self._cursor]))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))


class ProviderPicker(Static):
    """Keyboard-driven picker for built-in API providers."""

    can_focus = True

    DEFAULT_CSS = """
    ProviderPicker {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    ProviderPicker:focus { border: solid #72c7d4; }
    """

    class Selected(Message):
        # 初始化 Provider 选择消息
        def __init__(self, picker: ProviderPicker, provider: str) -> None:
            self.picker = picker
            self.provider = provider
            super().__init__()

    class Dismissed(Message):
        # 初始化 Provider 选择器关闭消息
        def __init__(self, picker: ProviderPicker) -> None:
            self.picker = picker
            super().__init__()

    # 初始化内置 Provider 列表并定位当前配置
    def __init__(self, providers: tuple[ProviderPreset, ...], current: str) -> None:
        super().__init__("")
        self._providers = providers
        self._current = current
        self._cursor = next(
            (
                index
                for index, provider in enumerate(providers)
                if provider.id == current
            ),
            0,
        )

    # 挂载时设置标题和键盘焦点
    def on_mount(self) -> None:
        self.border_title = " API Provider "
        self.border_subtitle = " ↑↓ move   Enter continue   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 渲染四种内置 API 接入方式
    def _render_ui(self) -> str:
        lines: list[str] = []
        for index, provider in enumerate(self._providers):
            current = "  [cyan]current[/cyan]" if provider.id == self._current else ""
            name = escape(provider.name)
            description = escape(provider.description)
            if index == self._cursor:
                lines.append(
                    f"[bold #72c7d4]❯[/bold #72c7d4] "
                    f"[bold white]{name}[/bold white]  [dim]{description}[/dim]{current}"
                )
            else:
                lines.append(
                    f"  [#c6cad0]{name}[/#c6cad0]  [dim]{description}[/dim]{current}"
                )
        return "\n".join(lines)

    # 处理上下移动、确认 Provider 和关闭快捷键
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._providers)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._providers)
            self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            self.post_message(self.Selected(self, self._providers[self._cursor].id))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))
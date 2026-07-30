from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label, Markdown, Static, TextArea

from code_rook.core.authority import AuthorityProfile, RuntimeMode
from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.provider_presets import (
    PROVIDER_PRESETS,
    ProviderPreset,
    discover_models,
)
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.transport.auth import read_ipc_token
from code_rook.core.transport.socket_client import IpcError, SocketClient
from code_rook.tui.clipboard import copy_to_windows_clipboard

log = logging.getLogger(__name__)


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s




def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 生成默认可见的工具结果预览，完整内容仍可点击展开
def _output_preview(output: str, *, max_lines: int = 3, max_chars: int = 360) -> str:
    lines = [line.rstrip() for line in output.strip().splitlines() if line.strip()]
    preview = "\n".join(lines[:max_lines])
    truncated = len(lines) > max_lines or len(preview) > max_chars
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip()
    if not preview:
        return "(no output)"
    return preview + ("\n..." if truncated else "")


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "apply_patch": ("patch",),
        "checkpoint_list": (),
        "checkpoint_rewind": ("checkpoint_id",),
        "read_file": ("path",),
        "edit_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "glob": ("pattern", "path"),
        "git_diff": ("scope", "path"),
        "grep": ("pattern", "path", "glob"),
        "bash": ("command",),
        "note_save": ("content",),
        "task_claim": ("task_id", "owner"),
        "task_create": ("subject",),
        "task_update": ("task_id", "status"),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


class LLMStreamBlock(Widget):
    """流式阶段显示纯文本，结束后切换为可选择的 Textual Markdown。"""

    DEFAULT_CSS = """
    LLMStreamBlock { height: auto; color: $text; }
    LLMStreamBlock > .message-kind { padding: 0 2; color: $text-muted; }
    LLMStreamBlock > .stream-text { padding: 0 2; color: $text; }
    """

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__()
        self._text = ""
        self._finalized = False
        self._kind = "working"

    # 根据流式状态挂载纯文本或支持屏幕选择的 Markdown 子组件
    def compose(self) -> ComposeResult:
        yield Static(f"[dim]{self._kind}[/dim]", classes="message-kind")
        if self._finalized:
            if self._text.strip():
                yield Markdown(self._text, classes="assistant-response")
            return
        yield Static(self._text, classes="stream-text", markup=False)

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        if self.is_attached:
            try:
                self.query_one(".stream-text", Static).update(self._text)
            except NoMatches:
                self.refresh(recompose=True)

    # 将当前可见模型消息标记为意图说明或最终回答
    def set_kind(self, kind: str) -> None:
        self._kind = kind
        if self.is_attached:
            try:
                self.query_one(".message-kind", Static).update(f"[dim]{kind}[/dim]")
            except NoMatches:
                self.refresh(recompose=True)

    # 返回当前流式块已累积的原始 Markdown 文本
    @property
    def text(self) -> str:
        return self._text

    # 将累积文本切换为 Textual Markdown，使最终内容可选择和复制
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self.is_attached:
            self.refresh(recompose=True)


class ToolCallBlock(Widget):
    """可折叠的工具调用块：折叠时显示摘要，点击后展开完整 params 和 output。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 2; color: $text-muted; }
    ToolCallBlock > .summary { color: $text-muted; }
    ToolCallBlock > .preview { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.finished > .preview { display: block; }
    ToolCallBlock.expanded > .preview { display: none; }
    ToolCallBlock > .detail { display: none; padding: 0 2 0 4; color: $text-muted; }
    ToolCallBlock.expanded > .detail { display: block; }
    """

    # 初始化工具调用信息
    def __init__(self, tool_name: str, params: dict[str, Any]) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        yield Static("", classes="preview", markup=False)
        yield Static("", classes="detail", markup=False)

    # 生成摘要行文本
    def _summary(self) -> str:
        if self._tool_name == "note_save" and self._finished and not self._is_error:
            return f"  [green]remembered[/green]  [dim]{self._elapsed_ms}ms[/dim]"

        params_pre = escape(_param_summary(self._tool_name, self._params))
        planning_tools = {"task_claim", "task_create", "task_update"}
        label = "plan" if self._tool_name in planning_tools else "tool"
        line = f"  [dim]{label}[/dim] [bold]{escape(self._tool_name)}[/bold]"
        if params_pre:
            line += f"  [dim]{params_pre}[/dim]"
        if self._finished:
            color = "red" if self._is_error else "green"
            status = "failed" if self._is_error else "done"
            hint = "  [dim](click for full output)[/dim]" if self._output else ""
            line += f"  [{color}]{status}[/{color}]  [dim]{self._elapsed_ms}ms[/dim]{hint}"
        else:
            line += "  [yellow]running[/yellow]"
        return line

    # 工具调用完成时更新结果并刷新摘要（widget 未挂载时跳过 DOM 更新）
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        if self.children:
            self.query_one(".summary", Static).update(self._summary())
            self.query_one(".preview", Static).update(_output_preview(output))
            self.add_class("finished")

    # 点击时切换展开/折叠状态
    def on_click(self) -> None:
        if not self._finished:
            return
        if "expanded" in self.classes:
            self.remove_class("expanded")
        else:
            detail = self.query_one(".detail", Static)
            detail.update(
                f"params\n{self._params_full}\n\n"
                f"output\n{self._output}\n\n"
                f"elapsed: {self._elapsed_ms}ms"
            )
            self.add_class("expanded")


class PermissionSelect(Static):
    """Compact inline approval prompt with full high-risk request context."""

    can_focus = True

    DEFAULT_CSS = """
    PermissionSelect {
        height: auto;
        margin: 1 2 0 2;
        padding: 1 2 1 2;
        border-left: thick #d5a84b;
        background: #181b20;
        color: $text;
    }
    PermissionSelect:focus {
        border-left: thick #f0c674;
        background: #1b1f24;
    }
    """

    _CHOICES: tuple[tuple[str, str, str, str], ...] = (
        ("allow_once", "Allow once", "1", "this request only"),
        ("always_allow", "Always allow", "2", "remember for future sessions"),
        ("deny_once", "Deny", "3", "skip this request"),
        ("always_deny", "Always deny", "4", "remember for future sessions"),
    )
    _KEY_MAP: dict[str, str] = {
        "y": "allow_once",  "1": "allow_once",
        "a": "always_allow","2": "always_allow",
        "n": "deny_once",   "3": "deny_once",
        "d": "always_deny", "4": "always_deny",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(self, widget: PermissionSelect, tool_use_id: str, decision: str) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            super().__init__()

    # 初始化控件，存储工具 ID（用于 IPC 回复）
    def __init__(
        self,
        tool_use_id: str,
        tool_name: str,
        param_preview: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("")
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._params = params or {}
        self._cursor = 0

    def on_mount(self) -> None:
        self.update(self._render_ui())
        self.focus()
        log.debug(
            "PermissionSelect.on_mount  can_focus=%s  focused_after=%r",
            self.can_focus,
            self.app.focused,
        )
        self.app.call_after_refresh(self._log_deferred_focus)

    # 在下一帧记录焦点是否真正转移到本控件
    def _log_deferred_focus(self) -> None:
        log.debug(
            "PermissionSelect.deferred_focus  app.focused=%r  has_focus=%s  focusable=%s",
            self.app.focused,
            self.has_focus,
            self.focusable,
        )

    # 焦点到达时记录，用于确认 focus() 是否真正生效
    def on_focus(self, event: events.Focus) -> None:
        log.debug(
            "PermissionSelect.on_focus  has_focus=%s  app.focused=%r",
            self.has_focus,
            self.app.focused,
        )

    # 焦点离开时记录，用于追踪是否被其他控件抢走焦点
    def on_blur(self, event: events.Blur) -> None:
        log.debug("PermissionSelect.on_blur  app.focused=%r", self.app.focused)

    def _request_context(self) -> tuple[str, str]:
        """Return a concise label and the exact security-relevant value."""
        if self._tool_name == "bash" and "command" in self._params:
            return "COMMAND", str(self._params["command"])
        if self._tool_name in {"write_file", "edit_file", "read_file"}:
            return "TARGET", str(self._params.get("path", self._param_preview))
        if self._tool_name == "checkpoint_rewind":
            return "CHECKPOINT", str(
                self._params.get("checkpoint_id", self._param_preview)
            )
        if self._tool_name == "spawn_agent":
            value = self._params.get("description", self._params.get("goal"))
            return "TASK", str(value if value is not None else self._param_preview)
        return "REQUEST", self._param_preview or "No additional details"

    def _action_label(self) -> str:
        labels = {
            "bash": "run a shell command",
            "write_file": "write a file",
            "edit_file": "edit a file",
            "apply_patch": "apply workspace changes",
            "checkpoint_rewind": "rewind workspace changes",
            "spawn_agent": "start a subagent",
        }
        return labels.get(self._tool_name, f"use {self._tool_name}")

    @staticmethod
    def _safe_lines(value: str) -> list[str]:
        sanitized = "".join(
            char if char in "\n\t" or ord(char) >= 32 else "?" for char in value
        )
        return sanitized.splitlines() or [""]

    # 生成包含完整高风险请求、决策层级和快捷键的审批面板
    def _render_ui(self) -> str:
        tool_name = escape(self._tool_name)
        context_label, context_value = self._request_context()
        lines = [
            "[bold #e7b95e]![/bold #e7b95e]  "
            "[bold white]Approval required[/bold white]",
            f"[dim]CodeRook wants to {escape(self._action_label())}[/dim]  "
            f"[#9aa4b2]{tool_name}[/#9aa4b2]",
            "",
            f"[bold #7d8794]{context_label}[/bold #7d8794]",
        ]
        for value_line in self._safe_lines(context_value):
            lines.append(
                f"[#56606d]│[/#56606d] [#e1e7ef]{escape(value_line)}[/#e1e7ef]"
            )
        lines.extend(("", "[bold white]Allow this action?[/bold white]"))
        for i, (_, label, key_hint, description) in enumerate(self._CHOICES):
            if i == self._cursor:
                lines.append(
                    f"[bold #79c7d3]❯[/bold #79c7d3] "
                    f"[bold #0f1419 on #79c7d3] {key_hint} [/bold #0f1419 on #79c7d3] "
                    f"[bold white]{label}[/bold white]  [#89929e]{description}[/#89929e]"
                )
            else:
                lines.append(
                    f"   [bold #6f7884]{key_hint}[/bold #6f7884]  "
                    f"[#c7cdd5]{label}[/#c7cdd5]  "
                    f"[#6f7884]{description}[/#6f7884]"
                )
        lines.extend(
            (
                "",
                "[#68717d]↑↓ navigate   Enter select   Esc deny[/#68717d]",
            )
        )
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            self._pick(self._CHOICES[self._cursor][0])
        elif key == "escape":
            event.stop()
            self._pick("deny_once")
        else:
            decision = self._KEY_MAP.get(key)
            if decision is not None:
                event.stop()
                self._pick(decision)

    # 发布决策消息，由宿主 App 负责 IPC 回复和控件清理
    def _pick(self, decision: str) -> None:
        log.debug("PermissionSelect._pick  decision=%s", decision)
        self.post_message(self.Decided(self, self._tool_use_id, decision))


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once": "allowed once",
        "always_allow": "always allowed",
        "deny_once": "denied",
        "always_deny": "always denied",
        "timeout": "timed out",
    }
    LABEL_MAP = _LABEL_MAP

    # 子类提交消息：用户作出权限决策时发布
    class Resolved(Message):
        def __init__(self, block: PermissionBlock, decision: str) -> None:
            self.block = block
            self.decision = decision
            super().__init__()

    # 初始化审批块，记录工具 ID、名称和参数预览
    def __init__(self, tool_use_id: str, tool_name: str, param_preview: str) -> None:
        self._tool_use_id = tool_use_id
        self._tool_name = tool_name
        self._param_preview = param_preview
        self._resolved = False
        super().__init__(self._pending_text(), classes="log-line permission-pending")

    def _pending_text(self) -> str:
        tool_name = escape(self._tool_name)
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        return (
            f"[bold yellow]! approval required[/bold yellow]  "
            f"[bold]{tool_name}[/bold]{preview}"
        )

    # 将块收缩为单行摘要并发布 Resolved 消息
    def _resolve(self, decision: str) -> None:
        if self._resolved:
            return
        self._resolved = True
        self.remove_class("permission-pending")
        allowed = decision in ("allow_once", "always_allow")
        icon = "[bold green]allowed[/bold green]" if allowed else "[bold red]denied[/bold red]"
        label = self._LABEL_MAP.get(decision, decision)
        tool_name = escape(self._tool_name)
        preview = f"  [dim]{escape(self._param_preview)}[/dim]" if self._param_preview else ""
        self.update(
            f"{icon} [bold]{tool_name}[/bold]  [dim]{label}[/dim]{preview}"
        )
        self.post_message(self.Resolved(self, decision))


_PERMISSION_PRESETS = (
    (
        "ask",
        RuntimeMode.ACT,
        AuthorityProfile.ASK,
        "询问后修改",
        "文件修改、命令和外部操作按策略确认",
    ),
    (
        "accept_edits",
        RuntimeMode.ACT,
        AuthorityProfile.AUTO_REVIEW,
        "自动接受修改",
        "工作区文件修改自动执行，命令和外部操作仍确认",
    ),
    (
        "plan",
        RuntimeMode.PLAN,
        AuthorityProfile.ASK,
        "Plan Mode",
        "只读分析并生成计划，批准后才实施",
    ),
)


class PermissionModePicker(Static):
    can_focus = True

    DEFAULT_CSS = """
    PermissionModePicker {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    PermissionModePicker:focus { border: solid #72c7d4; }
    """

    class Selected(Message):
        # 初始化权限模式选择消息
        def __init__(self, picker: PermissionModePicker, preset: str) -> None:
            self.picker = picker
            self.preset = preset
            super().__init__()

    class Dismissed(Message):
        # 初始化权限模式关闭消息
        def __init__(self, picker: PermissionModePicker) -> None:
            self.picker = picker
            super().__init__()

    # 初始化权限模式选择器并定位当前模式
    def __init__(self, current: str) -> None:
        super().__init__("")
        names = [item[0] for item in _PERMISSION_PRESETS]
        self._current = current
        self._cursor = names.index(current) if current in names else 0

    # 挂载后显示三种常用权限模式并取得焦点
    def on_mount(self) -> None:
        self.border_title = " Permissions "
        self.border_subtitle = " ↑↓ move   Enter select   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 渲染当前权限模式及其安全边界
    def _render_ui(self) -> str:
        lines = ["[bold]选择后续消息使用的权限模式[/bold]"]
        for index, (preset, _mode, _profile, label, detail) in enumerate(
            _PERMISSION_PRESETS
        ):
            marker = "[bold #72c7d4]>[/bold #72c7d4]" if index == self._cursor else " "
            current = "  [cyan]current[/cyan]" if preset == self._current else ""
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{marker} [{style}]{escape(label)}[/{style}]"
                f"  [dim]{escape(detail)}[/dim]{current}"
            )
        lines.append("[dim]也可用 Shift+Tab 在三种模式间循环[/dim]")
        return "\n".join(lines)

    # 处理权限模式的键盘导航、选择和关闭
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(_PERMISSION_PRESETS)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(_PERMISSION_PRESETS)
            self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            self.post_message(
                self.Selected(self, _PERMISSION_PRESETS[self._cursor][0])
            )
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))


class PlanReview(Static):
    """Keyboard-driven review prompt shown after a read-only planning run."""

    can_focus = True
    _CHOICES = (
        ("approve", "批准并实施", "退出 Plan Mode，按当前权限逐项执行"),
        ("revise", "继续规划", "输入反馈后再次进行只读分析"),
        ("cancel", "取消", "保留计划但不执行任何改动"),
    )

    DEFAULT_CSS = """
    PlanReview {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    PlanReview:focus { border: solid #72c7d4; }
    """

    class Decided(Message):
        # 初始化计划审阅决定消息
        def __init__(self, review: PlanReview, decision: str) -> None:
            self.review = review
            self.decision = decision
            super().__init__()

    # 初始化计划审阅面板并默认选中批准
    def __init__(self, run_id: str) -> None:
        super().__init__("")
        self.run_id = run_id
        self._cursor = 0

    # 挂载后渲染选项并取得键盘焦点
    def on_mount(self) -> None:
        self.border_title = " Plan ready "
        self.border_subtitle = " ↑↓ move   Enter select   Esc cancel "
        self.update(self._render_ui())
        self.focus()

    # 渲染批准、继续规划和取消三个明确分支
    def _render_ui(self) -> str:
        lines = ["[bold]计划已完成，下一步？[/bold]"]
        for index, (_decision, label, detail) in enumerate(self._CHOICES):
            marker = "[bold #72c7d4]>[/bold #72c7d4]" if index == self._cursor else " "
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{marker} [{style}]{escape(label)}[/{style}]  [dim]{escape(detail)}[/dim]"
            )
        return "\n".join(lines)

    # 处理计划审阅的键盘导航与确认
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            self.post_message(self.Decided(self, self._CHOICES[self._cursor][0]))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Decided(self, "cancel"))


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
        self.border_subtitle = " ↑↓ move   Enter open   Esc close "
        self.update(self._render_ui())
        self.focus()

    def _render_ui(self) -> str:
        if not self._sessions:
            return "[dim]No saved chat sessions.[/dim]"
        lines: list[str] = []
        for index, session in enumerate(self._sessions):
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
        return "\n".join(lines)

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k") and self._sessions:
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._sessions)
            self.update(self._render_ui())
        elif event.key in ("down", "j") and self._sessions:
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._sessions)
            self.update(self._render_ui())
        elif event.key == "enter" and self._sessions:
            event.stop()
            session_id = str(self._sessions[self._cursor].get("session_id", ""))
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

    # 初始化，接收全量 (name, description) 列表
    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__("")
        self._all_items = items
        self._filtered: list[tuple[str, str]] = list(items)
        self._cursor = 0

    # 根据查询字符串筛选列表，重置光标并重新渲染
    def set_query(self, query: str) -> None:
        q = query.lower()
        self._filtered = [(n, d) for n, d in self._all_items if not q or q in n.lower()]
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
            self.post_message(self.Selected(self._filtered[self._cursor][0]))

    # 返回当前是否有可选项
    def has_selection(self) -> bool:
        return len(self._filtered) > 0

    # 判断查询是否已经完整匹配一条命令，供 Enter 直接执行
    def has_exact_match(self, query: str) -> bool:
        return any(name == query for name, _description in self._filtered)

    def on_mount(self) -> None:
        self._redraw()

    # 渲染筛选后的命令列表，高亮当前光标项
    def _redraw(self) -> None:
        if not self._filtered:
            self.update("[dim]  no matching commands[/dim]")
            return
        lines: list[str] = []
        for i, (name, desc) in enumerate(self._filtered):
            desc_part = f"  [dim]{desc}[/dim]" if desc else ""
            if i == self._cursor:
                lines.append(f"  [bold cyan]❯ /{name}[/bold cyan]{desc_part}")
            else:
                lines.append(f"    [cyan]/{name}[/cyan]{desc_part}")
        lines.append("[dim]  ↑↓ navigate   tab complete   enter run/complete   esc dismiss[/dim]")
        self.update("\n".join(lines))


class ChatTextArea(TextArea):
    """支持 Enter 提交、Cmd/Shift/Alt+Enter 换行的多行聊天输入框。"""

    DEFAULT_CSS = """
    ChatTextArea {
        height: auto;
        min-height: 3;
        max-height: 12;
        border: round $surface-lighten-2;
        background: $background;
        padding: 0 1;
        margin: 1 2;
        scrollbar-size-vertical: 1;
    }
    ChatTextArea:focus {
        border: round $accent;
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
        await super()._on_key(event)


@dataclass(frozen=True)
class ModelSwitch:
    model: str
    session_id: str | None


@dataclass(frozen=True)
class ConfigSwitch:
    provider: str
    api_key: str = field(repr=False)
    model: str
    models: tuple[str, ...]
    session_id: str | None


class CodeRookTuiApp(App[ModelSwitch | ConfigSwitch | None]):
    """CodeRook TUI：终端滚屏风格，实时展示 agent 执行过程。"""

    TITLE = "CodeRook"
    BINDINGS = [
        Binding("ctrl+c", "copy_or_cancel", "copy / cancel", show=False),
        Binding("ctrl+shift+c", "copy_selection", "copy selection", show=False),
        Binding(
            "shift+tab",
            "cycle_permission_mode",
            "cycle permission mode",
            show=False,
            priority=True,
        ),
        Binding("ctrl+q", "quit", "quit"),
    ]
    CSS = """
    Screen { background: $background; }
    #header {
        height: 1;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    #log-view {
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
    }
    #banner { padding: 1 2 0 2; }
    Static.user-turn { color: $text; padding: 1 2 0 2; }
    Static.run-header { color: $text-muted; padding: 1 2 0 2; }
    Static.step-divider { color: $text-muted; padding: 0 2; }
    Static.run-ok { color: green; padding: 0 2 1 2; }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.usage { padding: 0 2; }
    Static.log-line { padding: 0 2; }
    Static.permission-pending { display: none; }
    Markdown.history-assistant { color: $text; }
    """

    _BANNER = (
        "[bold cyan]"
        " ██████╗ ██████╗ ██████╗ ███████╗    ██████╗  ██████╗  ██████╗ ██╗  ██╗\n"
        "██╔════╝██╔═══██╗██╔══██╗██╔════╝    ██╔══██╗██╔═══██╗██╔═══██╗██║ ██╔╝\n"
        "██║     ██║   ██║██║  ██║█████╗      ██████╔╝██║   ██║██║   ██║█████╔╝ \n"
        "██║     ██║   ██║██║  ██║██╔══╝      ██╔══██╗██║   ██║██║   ██║██╔═██╗ \n"
        "╚██████╗╚██████╔╝██████╔╝███████╗    ██║  ██║╚██████╔╝╚██████╔╝██║  ██╗\n"
        " ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝    ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝"
        "[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  键入 / 触发 skill  ·  拖选后 Ctrl+C 复制"
        "  ·  /copy 复制上一条回复  ·  Ctrl+Q 退出[/dim]"
    )

    # 初始化连接参数和 TUI 内部状态
    def __init__(
        self,
        host: str,
        port: int,
        replay_run_id: str | None = None,
        resume_session_id: str | None = None,
        auth_token: str | None = None,
        provider: str = "",
        model: str = "",
        models: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._resume_session_id = resume_session_id
        self._auth_token = auth_token
        self._provider = provider
        self._model = model
        self._models = models or ([model] if model else [])
        self._config_provider: ProviderPreset | None = None
        self._pending_config_key: str | None = None
        self._discovered_config_models: tuple[str, ...] = ()
        self._history_loaded = False
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._active_run_id: str | None = None
        self._cancel_requested = False
        self._busy = False
        self._last_context_pct: float = 0.0
        self._last_assistant_text = ""
        self._authority_preset = "ask"
        self._input_runtime_mode = RuntimeMode.ACT
        self._plan_review_pending = False
        self._plan_session_id: str | None = None
        self._plan_request = ""
        self._slash_items: list[tuple[str, str]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time

    def compose(self) -> ComposeResult:
        yield Label("[bold]CodeRook[/bold]  [dim]connecting...[/dim]", id="header")
        yield VerticalScroll(id="log-view")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._BANNER, id="banner"))
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = True
        prompt.border_title = "connecting..."

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("sessions", "open saved session picker"),
            ("new", "start a new chat session"),
            ("model", "show or switch the active model"),
            ("config", "change LLM API, model, or key"),
            ("compact", "compress context window"),
            ("copy", "copy the latest assistant reply"),
            ("plan", "analyze read-only and review a plan before implementation"),
            ("permissions", "review or change the permission mode"),
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append((skill.name, desc))
        except Exception:
            pass
        return items

    # 根据 / 前缀查询字符串挂载、更新或移除自动补全弹窗
    def on_chat_text_area_slash_changed(self, event: ChatTextArea.SlashChanged) -> None:
        query = event.query
        if query is None:
            try:
                self.query_one(SlashCompleteWidget).remove()
            except NoMatches:
                pass
            return
        try:
            popup = self.query_one(SlashCompleteWidget)
            popup.set_query(query)
        except NoMatches:
            popup = SlashCompleteWidget(self._slash_items)
            self.mount(popup, before="#prompt")
            popup.set_query(query)

    # 用户选中自动补全项后将 /{name} 填入输入框并移除弹窗
    def on_slash_complete_widget_selected(self, event: SlashCompleteWidget.Selected) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.text = f"/{event.skill_name} "
            prompt.move_cursor(prompt.document.end)
        try:
            self.query_one(SlashCompleteWidget).remove()
        except NoMatches:
            pass

    # 记录按键焦点；当 PermissionSelect 失去焦点后作为兜底处理权限快捷键
    def on_key(self, event: events.Key) -> None:
        log.debug("App.on_key  key=%r  focused=%r", event.key, self.focused)
        if not self._pending_permission_blocks:
            return
        try:
            select = self.query_one(PermissionSelect)
            if select.has_focus:
                return  # PermissionSelect 有焦点时自行处理，事件不会冒泡到这里
            key = event.key
            decision = PermissionSelect._KEY_MAP.get(key)
            if decision:
                event.stop()
                select._pick(decision)
            elif key in ("up", "k"):
                event.stop()
                select._cursor = (select._cursor - 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key in ("down", "j"):
                event.stop()
                select._cursor = (select._cursor + 1) % len(PermissionSelect._CHOICES)
                select.update(select._render_ui())
            elif key == "enter":
                event.stop()
                select._pick(PermissionSelect._CHOICES[select._cursor][0])
            elif key == "escape":
                event.stop()
                select._pick("deny_once")
        except Exception:
            pass

    # 在询问、自动接受修改和 Plan Mode 之间循环
    async def action_cycle_permission_mode(self) -> None:
        if self._busy or self._plan_review_pending:
            self._append(
                Static(
                    "[yellow]当前 run 或计划审阅完成后再切换权限模式[/yellow]",
                    classes="log-line",
                )
            )
            return
        names = [item[0] for item in _PERMISSION_PRESETS]
        index = (names.index(self._authority_preset) + 1) % len(names)
        await self._select_authority_preset(names[index])

    # 退出只断开界面，session 保留在 Core 中以便下次 resume
    async def action_quit(self) -> None:
        self.exit()

    async def action_cancel_run(self) -> None:
        if (
            self._client is None
            or self._active_run_id is None
            or not self._busy
            or self._cancel_requested
        ):
            return
        run_id = self._active_run_id
        self._cancel_requested = True
        self._append(Static(f"[yellow]cancelling {run_id}...[/yellow]", classes="log-line"))
        self.run_worker(self._do_cancel_run(run_id), name="cancel_run", exclusive=False)

    # 同时写入 Textual OSC 52 和 Windows 系统剪贴板，兼容不同终端
    def _write_clipboard(self, text: str) -> bool:
        if not text:
            return False
        self.copy_to_clipboard(text)
        if copy_to_windows_clipboard(text):
            log.debug("copied text through native Windows clipboard")
        return True

    # 复制当前屏幕选择；无有效选择时返回 False
    def _copy_selected_text(self) -> bool:
        return self._write_clipboard(self.screen.get_selected_text() or "")

    # 复制最近一段完整 assistant 文本并给出明确反馈
    def _copy_last_response(self) -> bool:
        if not self._write_clipboard(self._last_assistant_text):
            self.notify("暂无可复制的回复", severity="warning")
            return False
        self.notify("已复制上一条回复")
        return True

    # Ctrl+C 优先复制已选择文本，否则保持原有取消当前任务语义
    async def action_copy_or_cancel(self) -> None:
        if not self._copy_selected_text():
            await self.action_cancel_run()

    # Ctrl+Shift+C 优先复制选择，没有选择时复制上一条完整回复
    def action_copy_selection(self) -> None:
        if not self._copy_selected_text():
            self._copy_last_response()

    async def _do_cancel_run(self, run_id: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command("run.cancel", {"run_id": run_id})
        except (IpcError, RuntimeError, OSError) as exc:
            self._cancel_requested = False
            self._append(Static(f"[red]cancel error: {exc}[/red]", classes="log-line"))

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content:
            return
        if content == "/permissions":
            event.text_area.text = ""
            event.text_area.disabled = True
            event.text_area.border_title = "select a permission mode..."
            try:
                self.query_one(PermissionModePicker).remove()
            except NoMatches:
                pass
            self.mount(
                PermissionModePicker(self._authority_preset),
                before="#prompt",
            )
            return
        if content == "/plan":
            event.text_area.text = ""
            await self._select_authority_preset("plan", announce=False)
            return
        requested_mode = self._input_runtime_mode
        if content.startswith("/plan "):
            content = content.removeprefix("/plan ").strip()
            if not content:
                event.text_area.text = ""
                event.text_area.border_title = "plan mode — describe the task, then press Enter"
                self._input_runtime_mode = RuntimeMode.PLAN
                return
            requested_mode = RuntimeMode.PLAN
        if content == "/sessions":
            event.text_area.text = ""
            if self._client is not None and not self._busy:
                event.text_area.disabled = True
                event.text_area.border_title = "loading sessions..."
                self.run_worker(self._show_session_picker(), name="session_picker", exclusive=False)
            return
        if content == "/new":
            event.text_area.text = ""
            if self._client is not None and not self._busy:
                event.text_area.disabled = True
                event.text_area.border_title = "creating session..."
                self.run_worker(
                    self._create_and_switch_session(),
                    name="new_session",
                    exclusive=False,
                )
            return
        if content == "/model":
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]请先等待或取消当前任务再切换模型[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "select a model..."
            self.mount(ModelPicker(self._models, self._model), before="#prompt")
            return
        if content.startswith("/model "):
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]请先等待或取消当前任务再切换模型[/yellow]",
                        classes="log-line",
                    )
                )
                return
            selected = content.removeprefix("/model ").strip()
            if selected.startswith("add "):
                selected = selected.removeprefix("add ").strip()
            if selected:
                self.exit(ModelSwitch(model=selected, session_id=self._session_id))
            else:
                self._append(
                    Static(
                        "[yellow]用法：/model <模型 ID> 或 /model add <模型 ID>[/yellow]",
                        classes="log-line",
                    )
                )
            return
        if content == "/config":
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]请先等待或取消当前任务再修改 LLM 配置[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "select an API provider..."
            self.mount(
                ProviderPicker(PROVIDER_PRESETS, self._provider),
                before="#prompt",
            )
            return
        if content == "/copy":
            event.text_area.text = ""
            self._copy_last_response()
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(Static("[yellow]agent busy or disconnected[/yellow]", classes="log-line"))
            return
        self._begin_message(event.text_area, content, requested_mode)

    # 统一进入一次用户或计划批准触发的 run，确保输入状态与 mode 同步切换
    def _begin_message(
        self,
        prompt: ChatTextArea,
        content: str,
        runtime_mode: RuntimeMode,
        *,
        visible_content: str | None = None,
    ) -> None:
        self._busy = True
        prompt.text = ""
        prompt.disabled = True
        prompt.read_only = False
        prompt.border_title = (
            "agent is planning..." if runtime_mode == RuntimeMode.PLAN else "agent is working..."
        )
        shown = visible_content if visible_content is not None else content
        prefix = (
            "[bold cyan]plan >[/bold cyan]"
            if runtime_mode == RuntimeMode.PLAN
            else "[bold]>[/bold]"
        )
        self._append(Static(f"{prefix} {escape(shown)}", classes="user-turn"))
        self._update_header("planning" if runtime_mode == RuntimeMode.PLAN else "running")
        self.run_worker(
            self._do_send_message(content, runtime_mode),
            name="send_message",
            exclusive=False,
        )

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]compacting context...[/dim]", classes="log-line"))
        try:
            result = await self._client.send_command(
                "session.compact",
                {"session_id": self._session_id, "focus": ""},
            )
            summary_tokens = result.get("summary_tokens", 0)
            saved_tokens = result.get("saved_tokens", 0)
            retained_messages = result.get("retained_messages", 0)
            retained_tokens = result.get("retained_tokens", 0)
            quality = float(result.get("quality_score", 0.0))
            summary_path = str(result.get("summary_path", ""))
            original_tokens = int(result.get("original_tokens", 0))
            compacted_tokens = int(result.get("compacted_tokens", 0))
            if original_tokens > 0:
                self._last_context_pct *= compacted_tokens / original_tokens
            else:
                self._last_context_pct = 0.0
            self._append(Static(
                f"[bold cyan]Context compacted[/bold cyan]"
                f"  [dim]trigger=manual  summary={summary_tokens}  "
                f"retained={retained_messages} msgs/{retained_tokens} tokens  "
                f"saved≈{saved_tokens}  quality={quality:.0%}[/dim]",
                classes="log-line",
            ))
            if summary_path:
                self._append(Static(
                    f"[dim]  summary file: {summary_path}[/dim]",
                    classes="log-line",
                ))
        except (IpcError, RuntimeError, OSError) as e:
            self._append(Static(f"[red]compact error: {e}[/red]", classes="log-line"))

    def _restore_ready_prompt(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = False
            prompt.read_only = False
            if self._input_runtime_mode == RuntimeMode.PLAN:
                prompt.border_title = "plan mode — describe the task, then press Enter"
            else:
                prompt.border_title = (
                    "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                )
            prompt.focus()

    # 将 Core 返回的 authority 快照映射为 TUI 的三种常用权限模式
    def _apply_authority_snapshot(self, snapshot: dict[str, Any]) -> None:
        mode = RuntimeMode(str(snapshot.get("mode", RuntimeMode.ACT.value)))
        profile = AuthorityProfile(
            str(snapshot.get("profile", AuthorityProfile.ASK.value))
        )
        if mode == RuntimeMode.PLAN:
            preset = "plan"
        elif profile == AuthorityProfile.AUTO_REVIEW:
            preset = "accept_edits"
        else:
            preset = "ask"
        self._authority_preset = preset
        self._input_runtime_mode = mode

    # 读取当前会话的 authority，保证续接和切换会话后状态指示真实
    async def _refresh_authority(self) -> None:
        if self._client is None or self._session_id is None:
            return
        result = await self._client.send_command(
            "session.get_authority",
            {"session_id": self._session_id},
        )
        self._apply_authority_snapshot(dict(result.get("snapshot", {})))

    # 持久化并应用用户选择的权限预设
    async def _select_authority_preset(
        self,
        preset: str,
        *,
        announce: bool = True,
    ) -> None:
        selected = next(
            (item for item in _PERMISSION_PRESETS if item[0] == preset),
            None,
        )
        if selected is None:
            return
        _name, mode, profile, label, _detail = selected
        try:
            if self._client is not None and self._session_id is not None:
                result = await self._client.send_command(
                    "session.set_authority",
                    {
                        "session_id": self._session_id,
                        "mode": mode.value,
                        "profile": profile.value,
                    },
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._authority_preset = preset
                self._input_runtime_mode = mode
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(
                Static(f"[red]permission mode error: {escape(str(exc))}[/red]", classes="log-line")
            )
            self._restore_ready_prompt()
            return
        self._restore_ready_prompt()
        self._update_header("plan" if mode == RuntimeMode.PLAN else "ready")
        if announce:
            self._append(
                Static(
                    f"[bold cyan]权限模式[/bold cyan]  {escape(label)}",
                    classes="log-line",
                )
            )

    # 关闭权限模式选择器并恢复聊天输入
    async def on_permission_mode_picker_dismissed(
        self,
        message: PermissionModePicker.Dismissed,
    ) -> None:
        message.picker.remove()
        self._restore_ready_prompt()

    # 应用权限模式选择器中的模式并同步到 Core
    async def on_permission_mode_picker_selected(
        self,
        message: PermissionModePicker.Selected,
    ) -> None:
        message.picker.remove()
        await self._select_authority_preset(message.preset)

    async def _show_session_picker(self) -> None:
        if self._client is None:
            return
        try:
            result = await self._client.send_command(
                "session.list",
                {"include_closed": True, "limit": 10},
            )
            sessions = [
                session
                for session in result.get("sessions", [])
                if session.get("mode") == "chat"
            ]
            try:
                self.query_one(SessionPicker).remove()
            except NoMatches:
                pass
            self.mount(SessionPicker(sessions, self._session_id), before="#prompt")
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]session list error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    async def on_session_picker_dismissed(self, message: SessionPicker.Dismissed) -> None:
        message.picker.remove()
        self._restore_ready_prompt()

    async def on_session_picker_selected(self, message: SessionPicker.Selected) -> None:
        message.picker.remove()
        await self._switch_session(message.session_id)

    # 关闭模型选择器并恢复聊天输入框
    async def on_model_picker_dismissed(self, message: ModelPicker.Dismissed) -> None:
        message.picker.remove()
        self._config_provider = None
        self._pending_config_key = None
        self._discovered_config_models = ()
        self._restore_ready_prompt()

    # 选择模型后退出 TUI，由入口保存配置、重启 Core 并恢复会话
    async def on_model_picker_selected(self, message: ModelPicker.Selected) -> None:
        message.picker.remove()
        if self._config_provider is not None and self._pending_config_key is not None:
            self.exit(
                ConfigSwitch(
                    provider=self._config_provider.id,
                    api_key=self._pending_config_key,
                    model=message.model,
                    models=self._discovered_config_models,
                    session_id=self._session_id,
                )
            )
            return
        if message.model == self._model:
            self._restore_ready_prompt()
            return
        self.exit(ModelSwitch(model=message.model, session_id=self._session_id))

    # 关闭 Provider 选择器并恢复聊天输入
    async def on_provider_picker_dismissed(self, message: ProviderPicker.Dismissed) -> None:
        message.picker.remove()
        self._restore_ready_prompt()

    # 选择 Provider 后显示密码输入框
    async def on_provider_picker_selected(self, message: ProviderPicker.Selected) -> None:
        message.picker.remove()
        self._config_provider = next(
            provider for provider in PROVIDER_PRESETS if provider.id == message.provider
        )
        self.mount(ConfigApiKeyPrompt(self._config_provider), before="#prompt")

    # API Key 提交后异步探测该账号可用模型
    async def on_config_api_key_prompt_submitted(
        self,
        message: ConfigApiKeyPrompt.Submitted,
    ) -> None:
        self.run_worker(
            self._discover_config_models(message.prompt, message.api_key),
            name="config_models",
            exclusive=False,
        )

    # 取消 API Key 输入时返回 Provider 选择页
    async def on_config_api_key_prompt_dismissed(
        self,
        message: ConfigApiKeyPrompt.Dismissed,
    ) -> None:
        message.prompt.remove()
        self._config_provider = None
        self._discovered_config_models = ()
        self.mount(ProviderPicker(PROVIDER_PRESETS, self._provider), before="#prompt")

    # 调用 Models API 并在成功后挂载模型选择器
    async def _discover_config_models(
        self,
        prompt: ConfigApiKeyPrompt,
        api_key: str,
    ) -> None:
        provider = self._config_provider
        if provider is None:
            return
        try:
            models = await discover_models(provider, api_key)
        except ValueError as exc:
            prompt.show_error(str(exc))
            return
        self._pending_config_key = api_key
        self._discovered_config_models = tuple(models)
        prompt.remove()
        active = self._model if provider.id == self._provider else ""
        self.mount(ModelPicker(models, active), before="#prompt")

    async def _create_and_switch_session(self) -> None:
        if self._client is None:
            return
        try:
            created = await self._client.send_command("session.create", {"mode": "chat"})
            await self._load_session(str(created["session_id"]), resume=False)
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]session create error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    async def _switch_session(self, session_id: str) -> None:
        if self._client is None:
            return
        if session_id == self._session_id:
            self._restore_ready_prompt()
            return
        try:
            await self._client.send_command("session.resume", {"session_id": session_id})
            await self._load_session(session_id, resume=True)
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]session switch error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    async def _load_session(self, session_id: str, *, resume: bool) -> None:
        if self._client is None:
            return
        self._clear_plan_review()
        history = await self._client.send_command(
            "session.get_history",
            {"session_id": session_id},
        )
        log_view = self.query_one("#log-view", VerticalScroll)
        await log_view.remove_children()
        self._session_id = session_id
        self._resume_session_id = session_id
        self._history_loaded = True
        await self._refresh_authority()
        label = "resumed" if resume else "new session"
        self._append(
            Static(f"[bold cyan]{label}[/bold cyan]  [dim]{session_id}[/dim]", classes="log-line")
        )
        self._append_history(history.get("messages", []))
        self._update_header("ready")
        self._restore_ready_prompt()

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(
        self,
        content: str,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
    ) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "session.send_message",
                {
                    "session_id": self._session_id,
                    "content": content,
                    "runtime_mode": runtime_mode.value,
                },
            )
        except (IpcError, RuntimeError, OSError) as e:
            self._busy = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
            self._update_header("ready")
            self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

    # 处理计划批准、反馈或取消，只有批准分支会启动 Act run
    async def on_plan_review_decided(self, message: PlanReview.Decided) -> None:
        message.review.remove()
        self._plan_review_pending = False
        if message.decision == "approve":
            prompt = self._prompt()
            if (
                prompt is None
                or self._client is None
                or self._session_id is None
                or self._session_id != self._plan_session_id
            ):
                self._append(
                    Static("[red]计划所属会话已失效，未执行[/red]", classes="log-line")
                )
                self._plan_session_id = None
                self._plan_request = ""
                self._restore_ready_prompt()
                return
            original_request = self._plan_request
            self._plan_session_id = None
            self._plan_request = ""
            await self._select_authority_preset("ask", announce=False)
            self._begin_message(
                prompt,
                "Implement the approved plan from the immediately preceding planning turn. "
                "Re-check repository state before editing and report any required deviation."
                f"\n\nOriginal user request:\n{original_request}",
                RuntimeMode.ACT,
                visible_content="已批准计划，开始实施",
            )
            return
        self._plan_session_id = None
        self._plan_request = ""
        self._restore_ready_prompt()
        if message.decision == "revise":
            await self._select_authority_preset("plan", announce=False)
            prompt = self._prompt()
            if prompt is not None:
                prompt.text = "/plan "
                prompt.move_cursor(prompt.document.end)
                prompt.border_title = "plan mode — enter feedback, then press Enter"
            self._update_header("plan")
        else:
            await self._select_authority_preset("ask", announce=False)
            self._append(Static("[dim]计划已取消，未执行改动[/dim]", classes="log-line"))
            self._update_header("ready")

    # 处理内联审批控件的用户决策：发送 IPC 响应并恢复输入框
    async def on_permission_select_decided(self, msg: PermissionSelect.Decided) -> None:
        tool_use_id = msg.tool_use_id
        decision = msg.decision
        log.info("permission decided tool_use_id=%s decision=%s", tool_use_id, decision)
        try:
            msg.widget.remove()
            perm_block = self._pending_permission_blocks.pop(tool_use_id, None)
            if perm_block is not None:
                perm_block._resolve(decision)
            if self._client is not None:
                try:
                    await self._client.send_command(
                        "permission.respond",
                        {"tool_use_id": tool_use_id, "decision": decision},
                    )
                except (IpcError, RuntimeError, OSError):
                    pass
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget 并滚动到底部
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.mount(widget)
        log_view.scroll_end(animate=False)

    # 将恢复会话的历史消息转换为简洁的 TUI 块，工具结果仍由 Core 历史保留
    def _append_history(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        self._append(Static("[dim]── resumed conversation ──[/dim]", classes="log-line"))
        for message in messages:
            role = str(message.get("role", ""))
            content = message.get("content", "")
            if isinstance(content, str):
                if role == "user":
                    self._append(
                        Static(f"[bold]>[/bold] {escape(content)}", classes="user-turn")
                    )
                elif content.strip():
                    self._last_assistant_text = content.strip()
                    self._append(Markdown(content, classes="history-assistant"))
                continue

            if role != "assistant" or not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and str(block.get("text", "")).strip():
                    assistant_text = str(block["text"]).strip()
                    self._last_assistant_text = assistant_text
                    self._append(Markdown(assistant_text, classes="history-assistant"))
                elif block.get("type") == "tool_use":
                    tool_name = escape(str(block.get("name", "tool")))
                    params_raw = block.get("input", {})
                    params = params_raw if isinstance(params_raw, dict) else {}
                    summary = escape(_param_summary(str(block.get("name", "")), params))
                    self._append(
                        Static(
                            f"  [dim]tool[/dim] [bold]{tool_name}[/bold]  [dim]{summary}[/dim]",
                            classes="log-line",
                        )
                    )

    # 结束当前 LLM 流式块（下一个 token 将开启新块）
    def _break_llm(self) -> None:
        if self._current_llm is not None:
            if self._current_llm.text.strip():
                self._last_assistant_text = self._current_llm.text.strip()
            self._current_llm.finalize_markdown()
        self._current_llm = None

    # 清除当前计划审阅面板，防止切换会话后误批准旧计划
    def _clear_plan_review(self) -> None:
        self._plan_review_pending = False
        self._plan_session_id = None
        self._plan_request = ""
        try:
            self.query_one(PlanReview).remove()
        except NoMatches:
            pass

    # 将选择控件挂载到 Screen 顶层（#prompt 之前），避免 VerticalScroll 争抢焦点
    def _mount_permission_select(self, select: PermissionSelect) -> None:
        self.mount(select, before="#prompt")

    # 安全获取输入框，便于组件测试中未挂载时跳过 UI 操作
    def _prompt(self) -> ChatTextArea | None:
        try:
            return self.query_one("#prompt", ChatTextArea)
        except Exception:
            return None

    # 生成 context 占用率的彩色进度条字符串
    def _render_ctx_bar(self, pct: float) -> str:
        filled = int(pct * 20)
        bar = "█" * filled + "░" * (20 - filled)
        label = f"ctx:{pct * 100:.1f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 根据连接和运行状态刷新顶部标题
    def _update_header(self, state: str) -> None:
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        model = f"  [cyan]{escape(self._model)}[/cyan]" if self._model else ""
        permission_labels = {
            "ask": "ask",
            "accept_edits": "accept edits",
            "plan": "plan",
        }
        permission = (
            f"  [magenta]{permission_labels[self._authority_preset]}[/magenta]"
        )
        color = {
            "ready": "green",
            "running": "yellow",
            "planning": "cyan",
            "plan": "cyan",
            "plan ready": "cyan",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        header.update(
            f"[bold]CodeRook[/bold]  [dim]{self._host}:{self._port}[/dim]"
            f"{session}{model}{permission}  [{color}]{state}[/{color}]"
        )

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连
    async def _socket_loop(self) -> None:
        header = self.query_one("#header", Label)

        while True:
            client = SocketClient(self._host, self._port, auth_token=self._auth_token)
            self._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._update_header("disconnected")
                await asyncio.sleep(2)
                continue
            except IpcError as exc:
                log.error("IPC authentication failed: %s", exc)
                header.update(f"[bold]CodeRook[/bold]  [red]authentication failed: {exc}[/red]")
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._client = client
            self._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            async def on_event(event: dict[str, Any]) -> None:
                self._handle_event(event)

            client.on_event(on_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                params: dict[str, Any] = {
                    "topics": [
                        "session.*",
                        "run.*",
                        "step.*",
                        "agent.*",
                        "tool.*",
                        "llm.token",
                        "llm.usage",
                        "log.*",
                        "permission.*",
                        "context.*",
                        "subagent.*",
                        "skill.*",
                        "plan.*",
                    ],
                    "scope": "global",
                }
                if self._replay_run_id is not None:
                    params["replay_from_run"] = self._replay_run_id
                await client.send_command("event.subscribe", params)
                if self._resume_session_id is None:
                    created = await client.send_command("session.create", {"mode": "chat"})
                    self._session_id = str(created["session_id"])
                    self._resume_session_id = self._session_id
                    self._history_loaded = True
                    log.info("session created session_id=%s", self._session_id)
                else:
                    resumed = await client.send_command(
                        "session.resume", {"session_id": self._resume_session_id}
                    )
                    self._session_id = str(resumed["session"]["session_id"])
                    log.info("session resumed session_id=%s", self._session_id)
                    if not self._history_loaded:
                        history = await client.send_command(
                            "session.get_history", {"session_id": self._session_id}
                        )
                        self._append_history(history.get("messages", []))
                        self._history_loaded = True
                await self._refresh_authority()
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = self._plan_review_pending
                    prompt.read_only = False
                    if self._plan_review_pending:
                        prompt.border_title = "review the plan above"
                    elif self._input_runtime_mode == RuntimeMode.PLAN:
                        prompt.border_title = (
                            "plan mode — describe the task, then press Enter"
                        )
                    else:
                        prompt.border_title = (
                            "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        )
                        prompt.focus()
                self._update_header("plan ready" if self._plan_review_pending else "ready")
                await loop_task
            except IpcError as e:
                header.update(f"[bold]CodeRook[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._client = None
                self._session_id = None
                prompt = self._prompt()
                if prompt is not None:
                    prompt.disabled = True
                    prompt.read_only = False
                    prompt.border_title = "disconnected, retrying..."
                self._break_llm()
                await client.close()

            self._update_header("disconnected")
            await asyncio.sleep(2)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._handle_event_inner(event)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 实际的事件路由逻辑
    def _handle_event_inner(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "llm.token":
            token = event.get("token", "")
            if self._current_llm is None:
                llm_block = LLMStreamBlock()
                self._append(llm_block)
                self._current_llm = llm_block
            self._current_llm.append_token(token)
            return

        if t == "agent.decision":
            intent = str(event.get("intent") or "execute")
            summary = str(event.get("summary") or "")
            had_visible_text = (
                self._current_llm is not None and bool(self._current_llm._text.strip())
            )
            if self._current_llm is not None:
                self._current_llm.set_kind("answer" if intent == "respond" else intent)
            self._break_llm()
            if not had_visible_text and summary:
                self._append(
                    Static(
                        f"[dim]{escape(intent)}[/dim]  {escape(summary)}",
                        classes="log-line",
                    )
                )
            return

        if t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            ctx_bar = self._render_ctx_bar(pct)
            self._append(Static(
                f"[dim]  tokens  "
                f"in={event.get('input_tokens')} "
                f"out={event.get('output_tokens')} "
                f"cache={event.get('cache_read_input_tokens')}[/dim]"
                f"  {ctx_bar}",
                classes="usage",
            ))
            return

        self._break_llm()

        if t == "session.waiting_for_input":
            self._busy = False
            self._cancel_requested = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = self._plan_review_pending
                prompt.read_only = False
                if self._plan_review_pending:
                    prompt.border_title = "review the plan above"
                else:
                    prompt.border_title = (
                        "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                    )
                    prompt.focus()
            self._update_header("plan ready" if self._plan_review_pending else "ready")

        elif t == "plan.ready":
            run_id = str(event.get("run_id", ""))
            session_id = str(event.get("session_id", ""))
            if session_id != self._session_id:
                return
            self._plan_review_pending = True
            self._plan_session_id = session_id
            self._plan_request = str(event.get("request", ""))
            try:
                self.query_one(PlanReview).remove()
            except NoMatches:
                pass
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "review the plan above"
            self.mount(PlanReview(run_id), before="#prompt")
            self._update_header("plan ready")

        elif t == "session.interrupted":
            self._busy = False
            self._active_run_id = None
            self._cancel_requested = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "run cancelled - type a message to continue"
                prompt.focus()
            self._update_header("interrupted")

        elif t == "session.closed":
            self._busy = False
            self._cancel_requested = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "session closed"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = event.get("run_id", "")
            self._active_run_id = str(run_id)
            self._cancel_requested = False
            goal = event.get("goal", "")
            self._append(Static(
                f"[dim]run[/dim]  [cyan]{run_id}[/cyan]  [dim]{_preview(goal, 96)}[/dim]",
                classes="run-header",
            ))

        elif t == "skill.invoked":
            skill_name = event.get("skill_name", "")
            arguments = event.get("arguments", "")
            args_preview = _preview(arguments, 80) if arguments else ""
            args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
            self._append(Static(
                f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
                classes="log-line",
            ))

        elif t == "subagent.started":
            run_id = event.get("run_id", "")
            description = event.get("description", "")
            self._subagent_run_ids[run_id] = description
            self._subagent_start_times[run_id] = time.monotonic()
            short_id = run_id[:8] if len(run_id) >= 8 else run_id
            self._append(Static(
                f"[dim]┌─[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
                classes="log-line",
            ))

        elif t == "subagent.finished":
            run_id = event.get("run_id", "")
            status = event.get("status", "")
            description = self._subagent_run_ids.pop(run_id, event.get("description", ""))
            start = self._subagent_start_times.pop(run_id, None)
            elapsed = f"  [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
            desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
            if status == "success":
                self._append(Static(
                    f"[dim]└─[/dim] [bold green]done[/bold green] {desc_part}",
                    classes="log-line",
                ))
            else:
                self._append(Static(
                    f"[dim]└─[/dim] [bold red]failed[/bold red] {desc_part}",
                    classes="log-line",
                ))

        elif t == "background.started":
            job_id = str(event.get("job_id", ""))
            command = _preview(str(event.get("command", "")), 76)
            self._append(
                Static(
                    f"[dim]background[/dim]  [cyan]{job_id}[/cyan]  [dim]{command}[/dim]",
                    classes="log-line",
                )
            )

        elif t == "background.finished":
            job_id = str(event.get("job_id", ""))
            status = str(event.get("status", ""))
            marker = "[bold green]done[/bold green]" if status == "completed" else (
                "[bold red]failed[/bold red]"
            )
            self._append(
                Static(
                    f"{marker} [cyan]{job_id}[/cyan]  [dim]{status}[/dim]",
                    classes="log-line",
                )
            )

        elif t == "step.started":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            step = event.get("step", "")
            self._append(Static(
                f"[dim]step {step}  analyzing[/dim]",
                classes="step-divider",
            ))

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            params = event.get("params") or {}
            run_id = event.get("run_id", "")
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
            self._pending_tool_blocks[tool_use_id] = tc_block
            self._append(tc_block)

        elif t == "tool.call_finished":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            output = str(event.get("output") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(output, elapsed_ms)

        elif t == "tool.call_failed":
            tool_use_id = str(event.get("tool_use_id", ""))
            elapsed_ms = int(event.get("elapsed_ms") or 0)
            error_msg = str(event.get("error_message") or "")
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            status = event.get("status", "")
            steps = event.get("steps", 0)
            reason = event.get("reason") or ""
            self._active_run_id = None
            self._cancel_requested = False
            if status == "success":
                self._append(Static(
                    f"[bold green]completed[/bold green]  [dim]{steps} steps[/dim]",
                    classes="run-ok",
                ))
            elif reason == "cancelled":
                self._append(Static(
                    f"[bold yellow]cancelled[/bold yellow]  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[bold red]failed[/bold red]{detail}  [dim]{steps} steps[/dim]",
                    classes="run-err",
                ))

        elif t == "context.compacted":
            orig = event.get("original_tokens", 0)
            summary = event.get("summary_tokens", 0)
            compacted = event.get("compacted_tokens", 0)
            retained_messages = event.get("retained_messages", 0)
            retained_tokens = event.get("retained_tokens", 0)
            quality = float(event.get("quality_score", 0.0))
            trigger = event.get("trigger", "auto")
            summary_path = str(event.get("summary_path", ""))
            if int(orig) > 0:
                self._last_context_pct *= int(compacted) / int(orig)
            else:
                self._last_context_pct = 0.0
            self._append(Static(
                f"[bold cyan]Context compacted[/bold cyan]"
                f"  [dim]trigger={trigger}  original≈{orig} → compacted≈{compacted}  "
                f"summary={summary}  retained={retained_messages} msgs/{retained_tokens} tokens  "
                f"quality={quality:.0%}[/dim]",
                classes="log-line",
            ))
            if summary_path:
                self._append(Static(
                    f"[dim]  summary file: {summary_path}[/dim]",
                    classes="log-line",
                ))

        elif t == "permission.requested":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            raw_params = event.get("params", {})
            params = raw_params if isinstance(raw_params, dict) else {}
            try:
                _focused_repr = repr(self.focused)
            except Exception:
                _focused_repr = "?"
            log.info(
                "permission.requested tool=%s id=%s  app.focused=%s",
                tool_name, tool_use_id, _focused_repr,
            )
            perm_block = PermissionBlock(tool_use_id, tool_name, param_preview)
            self._pending_permission_blocks[tool_use_id] = perm_block
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = "permission required"
            self._append(perm_block)
            select = PermissionSelect(tool_use_id, tool_name, param_preview, params)
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t == "permission.denied":
            # 处理超时或断连等非用户交互触发的 deny。
            # 用户主动 deny 已由 on_permission_select_decided 处理。
            tool_use_id = str(event.get("tool_use_id", ""))
            decision = str(event.get("decision", "denied"))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                perm_block._resolve(decision)
                try:
                    select = self.query_one(PermissionSelect)
                    select.remove()
                except Exception:
                    pass
                if not self._pending_permission_blocks:
                    p = self._prompt()
                    if p is not None:
                        p.disabled = False
                        p.read_only = False
                        p.border_title = "type a message — enter to send, ⌘/⇧/⌥+enter for newline"
                        p.focus()

        elif t == "log.line":
            level = event.get("level", "INFO")
            color = "bold red" if level == "ERROR" else ("yellow" if level == "WARNING" else "dim")
            self._append(Static(
                f"[{color}]{level}[/{color}]  "
                f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
                classes="log-line",
            ))


# TUI 入口：读取配置并启动 CodeRookTuiApp
def run(
    config: CodeRookConfig,
    replay_run_id: str | None = None,
    resume_session_id: str | None = None,
) -> None:
    app = CodeRookTuiApp(
        config.host,
        config.port,
        replay_run_id=replay_run_id,
        resume_session_id=resume_session_id,
        auth_token=read_ipc_token(Path(config.ipc_token_file)),
    )
    app.run()

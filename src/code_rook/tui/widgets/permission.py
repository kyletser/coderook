"""权限审批相关控件。"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from rich.markup import escape
from textual import events
from textual.message import Message
from textual.widgets import Static

from code_rook.core.authority import AuthorityProfile, RuntimeMode

log = logging.getLogger(__name__)


# 从 old/new 文本构造带 ---/+++/@@/-/+ 的统一 diff 文本；内容相同返回空
def _render_inline_diff(old_text: str, new_text: str) -> str:
    old_lines = old_text.splitlines() or [""]
    new_lines = new_text.splitlines() or [""]
    if old_lines == new_lines:
        return ""
    return "\n".join(
        difflib.unified_diff(old_lines, new_lines, lineterm="")
    )


# 新文件写内容的前缀预览：每行加 + 号，形似 diff 的新增块
def _render_new_file_diff(content: str) -> str:
    lines = content.splitlines() or [""]
    return "\n".join(f"+ {line}" for line in lines)


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
        "p": "always_allow_pattern",
    }

    # 用户作出权限决策时发布，携带工具 ID 和决策字符串
    class Decided(Message):
        # 初始化决策消息，存储控件引用、工具 ID 和决策
        def __init__(
            self,
            widget: PermissionSelect,
            tool_use_id: str,
            decision: str,
            *,
            selected_hunks: list[str] | None = None,
            patch_plan_id: str | None = None,
        ) -> None:
            self.widget = widget
            self.tool_use_id = tool_use_id
            self.decision = decision
            self.selected_hunks = selected_hunks
            self.patch_plan_id = patch_plan_id
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
        context = self._params.get("_approval_context")
        context_dict = context if isinstance(context, dict) else {}
        raw_plan = context_dict.get("patch_plan")
        self._patch_plan = raw_plan if isinstance(raw_plan, dict) else None
        self._patch_plan_id = (
            str(self._patch_plan.get("id", "")) if self._patch_plan is not None else None
        )
        self._patch_hunks = self._collect_patch_hunks()
        self._selected_hunks = {
            str(hunk["id"]) for hunk in self._patch_hunks if hunk.get("id")
        }
        self._hunk_cursor = 0
        self._hunk_mode = bool(self._patch_hunks)
        # 仅 bash 追加"始终允许此命令模式"选项；其余工具保持原样
        self._choices = (
            self._CHOICES
            + (
                ("always_allow_pattern", "Always allow pattern", "5",
                 "remember this command prefix, see W3.2"),
            )
            if tool_name == "bash"
            else self._CHOICES
        )

    # 从审批上下文展平文件级 hunk，并补齐显示所需的路径和动作
    def _collect_patch_hunks(self) -> list[dict[str, Any]]:
        if self._patch_plan is None:
            return []
        collected: list[dict[str, Any]] = []
        files = self._patch_plan.get("files", [])
        if not isinstance(files, list):
            return []
        for raw_file in files:
            if not isinstance(raw_file, dict):
                continue
            hunks = raw_file.get("hunks", [])
            if not isinstance(hunks, list):
                continue
            for raw_hunk in hunks:
                if isinstance(raw_hunk, dict):
                    collected.append(
                        dict(
                            raw_hunk,
                            path=str(raw_file.get("path", "")),
                            action=str(raw_file.get("action", "modify")),
                        )
                    )
        return collected

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
        if self._tool_name in {"agent", "spawn_agent"}:
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
            "agent": "manage a durable worker",
            "spawn_agent": "start a subagent",
        }
        return labels.get(self._tool_name, f"use {self._tool_name}")

    # 依据编辑类工具参数生成嵌卡 diff 预览；无可呈现内容时返回空串
    def _diff_preview(self) -> str:
        t = self._tool_name
        if t == "edit_file":
            old_t = str(self._params.get("old_text", ""))
            new_t = str(self._params.get("new_text", ""))
            diff = _render_inline_diff(old_t, new_t)
            return diff if diff else ""
        if t == "write_file":
            return _render_new_file_diff(str(self._params.get("content", "")))
        if t == "apply_patch":
            return str(self._params.get("patch", ""))
        return ""

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
        diff = self._diff_preview()
        if self._patch_hunks:
            lines.extend(("", "[bold #7d8794]HUNKS[/bold #7d8794]"))
            for index, hunk in enumerate(self._patch_hunks[:30]):
                hunk_id = str(hunk.get("id", ""))
                checked = "x" if hunk_id in self._selected_hunks else " "
                cursor = "❯" if self._hunk_mode and index == self._hunk_cursor else " "
                path = escape(str(hunk.get("path", "")))
                source_start = int(hunk.get("source_start", 0))
                additions = int(hunk.get("additions", 0))
                removals = int(hunk.get("removals", 0))
                locked = " [dim](all-or-nothing)[/dim]" if not hunk.get(
                    "selectable", True
                ) else ""
                lines.append(
                    f"[bold #79c7d3]{cursor}[/bold #79c7d3] [{checked}] "
                    f"[#c7cdd5]{path}:{source_start}[/#c7cdd5] "
                    f"[green]+{additions}[/green]/[red]-{removals}[/red]{locked}"
                )
            if len(self._patch_hunks) > 30:
                lines.append("[dim]⋯ additional hunks are selected but not shown[/dim]")
        if diff:
            diff_lines = self._safe_lines(diff)
            lines.append("")
            lines.append("[bold #7d8794]DIFF[/bold #7d8794]")
            for diff_line in diff_lines[:50]:
                lines.append(f"[#56606d]·[/#56606d] {escape(diff_line)}")
            if len(diff_lines) > 50:
                lines.append("[dim]⋯ diff truncated, approve/deny to continue[/dim]")
        lines.extend(("", "[bold white]Allow this action?[/bold white]"))
        for i, (_, label, key_hint, description) in enumerate(self._choices):
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
        hint = "↑↓ navigate   Enter select   Esc deny"
        if self._patch_hunks:
            hint = "Tab hunks/actions   Space toggle hunk   " + hint
        lines.extend(("", f"[#68717d]{hint}[/#68717d]"))
        return "\n".join(lines)

    # 方向键导航；快捷键直接选择；enter 确认光标位置
    def on_key(self, event: events.Key) -> None:
        log.debug("PermissionSelect.on_key  key=%r  char=%r", event.key, event.character)
        key = event.key
        if self._patch_hunks and key == "tab":
            event.stop()
            self._hunk_mode = not self._hunk_mode
            self.update(self._render_ui())
        elif self._patch_hunks and self._hunk_mode and key == "space":
            event.stop()
            hunk = self._patch_hunks[self._hunk_cursor]
            hunk_id = str(hunk.get("id", ""))
            if hunk.get("selectable", True):
                if hunk_id in self._selected_hunks:
                    self._selected_hunks.remove(hunk_id)
                else:
                    self._selected_hunks.add(hunk_id)
            self.update(self._render_ui())
        elif key in ("up", "k"):
            event.stop()
            if self._hunk_mode:
                self._hunk_cursor = (self._hunk_cursor - 1) % len(self._patch_hunks)
            else:
                self._cursor = (self._cursor - 1) % len(self._choices)
            self.update(self._render_ui())
        elif key in ("down", "j"):
            event.stop()
            if self._hunk_mode:
                self._hunk_cursor = (self._hunk_cursor + 1) % len(self._patch_hunks)
            else:
                self._cursor = (self._cursor + 1) % len(self._choices)
            self.update(self._render_ui())
        elif key == "enter":
            event.stop()
            if self._hunk_mode:
                self._hunk_mode = False
                self.update(self._render_ui())
            else:
                self._pick(self._choices[self._cursor][0])
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
        selected: list[str] | None = None
        if self._patch_hunks:
            selected = sorted(self._selected_hunks)
            if decision in {"allow_once", "always_allow"} and not selected:
                decision = "deny_once"
            if decision == "always_allow" and len(selected) != len(self._patch_hunks):
                decision = "allow_once"
        self.post_message(
            self.Decided(
                self,
                self._tool_use_id,
                decision,
                selected_hunks=selected,
                patch_plan_id=self._patch_plan_id,
            )
        )


class PermissionBlock(Static):
    """日志里的权限审批摘要"""

    _LABEL_MAP: dict[str, str] = {
        "allow_once": "allowed once",
        "always_allow": "always allowed",
        "always_allow_pattern": "always allowed (pattern)",
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
        allowed = decision in ("allow_once", "always_allow", "always_allow_pattern")
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
        AuthorityProfile.ASK,
        "询问后修改",
        "文件修改、命令和外部操作按策略确认",
    ),
    (
        "accept_edits",
        AuthorityProfile.AUTO_REVIEW,
        "自动接受修改",
        "工作区文件修改自动执行，命令和外部操作仍确认",
    ),
    (
        "full_access",
        AuthorityProfile.FULL_ACCESS,
        "全自动执行",
        "本机命令、修改和外部操作自动批准，Plan Mode 与工具边界仍生效",
    ),
)

_MODE_CYCLE = (RuntimeMode.ACT, RuntimeMode.OPERATE, RuntimeMode.PLAN)


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

    # 挂载后显示三种权限姿态并取得焦点
    def on_mount(self) -> None:
        self.border_title = " Permissions "
        self.border_subtitle = " ↑↓ move   Enter select   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 渲染当前权限模式及其安全边界
    def _render_ui(self) -> str:
        lines = ["[bold]选择后续消息使用的权限模式[/bold]"]
        for index, (preset, _profile, label, detail) in enumerate(
            _PERMISSION_PRESETS
        ):
            marker = "[bold #72c7d4]>[/bold #72c7d4]" if index == self._cursor else " "
            current = "  [cyan]current[/cyan]" if preset == self._current else ""
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{marker} [{style}]{escape(label)}[/{style}]"
                f"  [dim]{escape(detail)}[/dim]{current}"
            )
        lines.append("[dim]也可用 Shift+Tab 在三种权限姿态间循环[/dim]")
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

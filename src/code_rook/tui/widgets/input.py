"""输入区相关控件：API Key 提示、斜杠命令补全与多行聊天输入框。"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
from code_rook.tui.product import tr

_INPUT_HISTORY_LIMIT = 500
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_HISTORY_DISABLED_VALUES = {"0", "false", "no", "off", "disabled"}
_SENSITIVE_HISTORY_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)


# 将纯文件路径粘贴识别为本地图片，普通文本返回 None
def _pasted_image_path(text: str) -> Path | None:
    candidate = text.strip().strip('"').strip("'")
    if not candidate or "\n" in candidate or "\r" in candidate:
        return None
    path = Path(candidate).expanduser()
    if path.suffix.casefold() not in _IMAGE_SUFFIXES or not path.is_file():
        return None
    return path.resolve()


# 为工作区路径生成不可碰撞且不暴露完整路径的历史分区键
def _workspace_history_key(workspace: Path | None = None) -> str:
    root = (workspace or Path.cwd()).resolve()
    digest = hashlib.sha256(str(root).casefold().encode("utf-8")).hexdigest()[:16]
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-.") or "workspace"
    return f"{label[:32]}-{digest}"


# 返回按工作区分区、但不会污染仓库的用户级输入历史路径
def _input_history_path(
    workspace: Path | None = None,
    *,
    state_root: Path | None = None,
) -> Path:
    base = state_root or Path.home() / ".coderook" / "tui"
    return base / "history" / f"{_workspace_history_key(workspace)}.jsonl"


# 返回当前工作区分区的 TUI 本地偏好文件路径
def _input_history_settings_path(
    workspace: Path | None = None,
    *,
    state_root: Path | None = None,
) -> Path:
    base = state_root or Path.home() / ".coderook" / "tui"
    return base / "settings" / f"{_workspace_history_key(workspace)}.json"


# 判断文本是否可能包含密钥，命中时整条输入不进入历史
def _is_sensitive_history(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _SENSITIVE_HISTORY_PATTERNS)


# 读取工作区历史开关，环境变量拥有最高优先级
def _input_history_enabled(workspace: Path | None = None) -> bool:
    override = os.environ.get("CODEROOK_TUI_HISTORY")
    if override is not None:
        return override.strip().casefold() not in _HISTORY_DISABLED_VALUES
    try:
        payload = json.loads(_input_history_settings_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return bool(payload.get("history_enabled", True)) if isinstance(payload, dict) else True


# 持久化当前工作区的历史开关，不触碰已有历史内容
def _set_input_history_enabled(enabled: bool, workspace: Path | None = None) -> None:
    path = _input_history_settings_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"history_enabled": enabled}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass


# 清空当前工作区的持久输入历史
def _clear_input_history(
    workspace: Path | None = None,
    *,
    state_root: Path | None = None,
) -> None:
    try:
        _input_history_path(workspace, state_root=state_root).unlink(missing_ok=True)
    except OSError:
        pass


# 从磁盘加载最近的工作区输入历史，坏行与敏感旧记录静默跳过
def _load_input_history(
    limit: int = _INPUT_HISTORY_LIMIT,
    *,
    path: Path | None = None,
    enabled: bool = True,
) -> list[str]:
    if not enabled:
        return []
    history_path = path if path is not None else _input_history_path()
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()[-limit:]
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
            if text and not _is_sensitive_history(text):
                history.append(text)
    return history


# 将一条非敏感输入追加到指定工作区历史，写入失败时静默跳过
def _save_input_history_entry(
    text: str,
    *,
    path: Path | None = None,
    enabled: bool = True,
) -> None:
    if not enabled or not text.strip() or _is_sensitive_history(text):
        return
    history_path = path if path is not None else _input_history_path()
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")
        try:
            history_path.chmod(0o600)
        except OSError:
            pass
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
    def __init__(self, provider: ProviderPreset, *, locale: str = "zh-CN") -> None:
        super().__init__()
        self.provider = provider
        self._locale = locale

    # 组合说明、密码输入框和错误提示
    def compose(self) -> ComposeResult:
        yield Label(
            f"[bold]{escape(self.provider.name)}[/bold]\n"
            f"[dim]{tr('input.config.intro', self._locale)}[/dim]"
        )
        yield Input(placeholder="API Key", password=True, id="config-api-key")
        yield Label("", classes="config-error", id="config-key-error")

    # 挂载时设置步骤提示并聚焦密码输入框
    def on_mount(self) -> None:
        self.border_title = " API Key "
        self.border_subtitle = f" {tr('input.config.hint', self._locale)} "
        self.query_one("#config-api-key", Input).focus()

    # 切换配置面板语言并重组说明和快捷提示
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        self.border_subtitle = f" {tr('input.config.hint', self._locale)} "
        self.refresh(recompose=True)

    # 校验密钥非空后发布提交消息
    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        api_key = event.value.strip()
        if not api_key:
            self.show_error(tr("input.config.empty", self._locale))
            return
        event.input.disabled = True
        self.border_subtitle = f" {tr('input.config.discovering', self._locale)} "
        self.post_message(self.Submitted(self, api_key))

    # 显示探测错误并允许用户重新输入
    def show_error(self, message: str) -> None:
        self.query_one("#config-key-error", Label).update(escape(message))
        key_input = self.query_one("#config-api-key", Input)
        key_input.disabled = False
        key_input.focus()
        self.border_subtitle = f" {tr('input.config.retry', self._locale)} "

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
    def __init__(self, items: list[CompletionItem], *, locale: str = "en-US") -> None:
        super().__init__("")
        self._all_items = list(items)
        self._filtered: list[CompletionItem] = list(items)
        self._cursor = 0
        self._locale = locale

    # 切换补全控件语言并立即重绘静态导航文案
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        if self.is_attached:
            self._redraw()

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
            self.update(f"[dim]  {tr('completion.no_match', self._locale)}[/dim]")
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
            usage_part = tr(
                "completion.usage",
                self._locale,
                name=selected.name,
                usage=selected.usage,
            )
        else:
            usage_part = selected.description
        lines.append(f"[dim]{usage_part}[/dim]")
        lines.append(f"[dim]  {tr('completion.hint', self._locale)}[/dim]")
        self.update("\n".join(lines))


_TRAILING_FILE_REFERENCE = re.compile(r'(?:^|\s)@(?:"([^"]*)|([^\s]*))$')


# 返回输入末尾正在编辑的 @文件 查询，其他位置或已结束引用返回 None
def _file_reference_query(text: str) -> str | None:
    match = _TRAILING_FILE_REFERENCE.search(text)
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else (match.group(2) or "")


# 用选定路径替换输入末尾的文件查询，含空格路径自动使用双引号包裹
def _complete_file_reference(text: str, path: str) -> str:
    token = f'@"{path}"' if any(char.isspace() for char in path) else f"@{path}"
    match = _TRAILING_FILE_REFERENCE.search(text)
    if match is None:
        return text
    return f"{text[:match.start()].rstrip()} {token} ".lstrip()


class FileCompleteWidget(Static):
    """工作区文件模糊补全弹窗，只返回路径而不读取文件正文。"""

    can_focus = False

    DEFAULT_CSS = """
    FileCompleteWidget {
        height: auto;
        max-height: 12;
        padding: 0 1;
        margin: 0 2;
        background: $surface;
        border: round $surface-lighten-2;
    }
    """

    class Selected(Message):
        # 初始化文件补全选择消息并携带工作区相对路径
        def __init__(self, path: str) -> None:
            self.path = path
            super().__init__()

    # 初始化完整路径清单并建立首屏候选
    def __init__(self, paths: list[str], *, locale: str = "en-US") -> None:
        super().__init__("")
        self._paths = list(paths)
        self._filtered = self._paths[:10]
        self._cursor = 0
        self._locale = locale

    # 按路径和文件名做模糊筛选并只展示前十项
    def set_query(self, query: str) -> None:
        matched: list[tuple[int, int, str]] = []
        for order, path in enumerate(self._paths):
            if _fuzzy_match(query, Path(path).name):
                matched.append((0, order, path))
            elif _fuzzy_match(query, path):
                matched.append((1, order, path))
        matched.sort(key=lambda item: (item[0], item[1]))
        self._filtered = [path for _, _, path in matched[:10]]
        self._cursor = min(self._cursor, max(0, len(self._filtered) - 1))
        if self.is_attached:
            self._redraw()

    # 向上循环移动文件候选光标
    def move_up(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor - 1) % len(self._filtered)
            self._redraw()

    # 向下循环移动文件候选光标
    def move_down(self) -> None:
        if self._filtered:
            self._cursor = (self._cursor + 1) % len(self._filtered)
            self._redraw()

    # 发布当前选中的工作区文件路径
    def select_current(self) -> None:
        if self._filtered:
            self.post_message(self.Selected(self._filtered[self._cursor]))

    # 判断当前是否存在可选择的文件候选
    def has_selection(self) -> bool:
        return bool(self._filtered)

    # 挂载后绘制第一批候选
    def on_mount(self) -> None:
        self._redraw()

    # 绘制简洁路径列表和键盘操作提示
    def _redraw(self) -> None:
        if not self._filtered:
            message = "没有匹配文件" if self._locale != "en-US" else "No matching files"
            self.update(f"[dim]  {message}[/dim]")
            return
        lines: list[str] = []
        for index, path in enumerate(self._filtered):
            marker = "[bold cyan]❯" if index == self._cursor else " "
            suffix = "[/bold cyan]" if index == self._cursor else ""
            lines.append(f"  {marker} @{escape(path)}{suffix}")
        hint = "↑↓ 选择 · Tab/Enter 插入 · Esc 关闭" if self._locale != "en-US" else (
            "↑↓ select · Tab/Enter insert · Esc close"
        )
        lines.append(f"[dim]  {hint}[/dim]")
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

    class FileReferenceChanged(Message):
        # 初始化文件引用查询变化消息，None 表示关闭候选
        def __init__(self, query: str | None) -> None:
            self.query = query
            super().__init__()

    class CycleMode(Message):
        pass

    class ImagePasted(Message):
        # 初始化图片粘贴消息并携带已解析的绝对路径
        def __init__(self, path: Path) -> None:
            self.path = path
            super().__init__()

    # 初始化输入历史状态，支持空输入时 ↑/↓ 回溯最近提交
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._history: list[str] = []
        self._history_index: int | None = None
        self._history_draft: str = ""
        self._history_path = _input_history_path()
        self._history_enabled = True

    # 设置可回溯的输入历史列表并重置回溯状态
    def set_history(
        self,
        history: list[str],
        *,
        path: Path | None = None,
        enabled: bool = True,
    ) -> None:
        self._history = list(history)
        self._history_index = None
        self._history_draft = ""
        if path is not None:
            self._history_path = path
        self._history_enabled = enabled

    # 开关当前输入框的历史记录，关闭时仍保留已有磁盘内容
    def set_history_enabled(self, enabled: bool) -> None:
        self._history_enabled = enabled
        self._history_index = None
        self._history_draft = ""

    # 清空内存与磁盘历史并复位导航位置
    def clear_history(self) -> None:
        self._history = []
        self._history_index = None
        self._history_draft = ""
        try:
            self._history_path.unlink(missing_ok=True)
        except OSError:
            pass

    # 记录一条提交输入：敏感内容不入内存或磁盘，其余内容连续去重
    def record_history(self, text: str) -> None:
        cleaned = text.strip()
        if (
            not self._history_enabled
            or not cleaned
            or _is_sensitive_history(cleaned)
            or (self._history and self._history[-1] == cleaned)
        ):
            return
        self._history.append(cleaned)
        if len(self._history) > _INPUT_HISTORY_LIMIT:
            self._history = self._history[-_INPUT_HISTORY_LIMIT:]
        self._history_index = None
        self._history_draft = ""
        _save_input_history_entry(
            cleaned,
            path=self._history_path,
            enabled=self._history_enabled,
        )

    # 拦截纯图片路径粘贴并交由 App 落入 ArtifactStore，普通文本沿用 TextArea 行为
    async def _on_paste(self, event: events.Paste) -> None:
        image_path = _pasted_image_path(event.text)
        if image_path is not None:
            event.stop()
            event.prevent_default()
            self.post_message(self.ImagePasted(image_path))
            return
        await super()._on_paste(event)

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
        self.post_message(ChatTextArea.FileReferenceChanged(_file_reference_query(text)))

    # Enter 提交；↑↓/Tab/Esc 路由到自动补全弹窗；Cmd/Shift/Alt+Enter 插入换行；其余键交回 TextArea
    async def _on_key(self, event: events.Key) -> None:
        key = event.key

        popup: SlashCompleteWidget | FileCompleteWidget | None = None
        try:
            popup = self.app.query_one(SlashCompleteWidget)
        except NoMatches:
            try:
                popup = self.app.query_one(FileCompleteWidget)
            except NoMatches:
                popup = None

        if key == "enter":
            event.stop()
            event.prevent_default()
            query = self.text[1:] if self.text.startswith("/") else ""
            if (
                isinstance(popup, SlashCompleteWidget)
                and popup.has_selection()
                and not popup.has_exact_match(query)
            ):
                popup.select_current()
                return
            if isinstance(popup, FileCompleteWidget) and popup.has_selection():
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
                if isinstance(popup, FileCompleteWidget):
                    self.post_message(ChatTextArea.FileReferenceChanged(query=None))
                else:
                    self.post_message(ChatTextArea.SlashChanged(query=None))
                return
        if key == "up" and popup is None and (not self.text or self._history_index is not None):
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

    # 把选定工作区路径写回当前 @查询并将光标移动到末尾
    def complete_file_reference(self, path: str) -> None:
        self.text = _complete_file_reference(self.text, path)
        self.move_cursor(self.document.end)

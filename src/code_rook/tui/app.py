from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Label, Markdown, Static, TextArea

from code_rook.core.authority import AuthorityProfile, RuntimeMode, WorkspaceTrust
from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.doctor import ProviderDoctor
from code_rook.core.llm.pricing import (
    cache_read_savings,
    estimate_cost,
    format_cost,
    get_pricing,
    load_pricing_overrides,
)
from code_rook.core.llm.provider_presets import (
    PROVIDER_PRESETS,
    ProviderPreset,
    discover_models,
)
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.llm.routes import ProviderRoute
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.skills.manager import (
    InstallScope,
    SkillConfirmationRequired,
    SkillManager,
    SkillManagerError,
)
from code_rook.core.transport.auth import read_ipc_token
from code_rook.core.transport.socket_client import IpcError, SocketClient
from code_rook.tui.clipboard import copy_to_windows_clipboard
from code_rook.tui.panels import (
    render_turn_inspector,
    render_workflow_graph,
    render_workflow_list,
)

log = logging.getLogger(__name__)

_PROMPT_READY = "消息"
_PROMPT_CONNECTING = "正在连接"
_PROMPT_PLAN = "规划模式"
_PROMPT_RUNNING = "执行中 · Enter 补充 · Ctrl+C 取消"
_PROMPT_PLANNING = "规划中 · Enter 补充 · Ctrl+C 取消"
_PROMPT_PERMISSION = "等待操作确认"
_PROMPT_QUESTION = "请回答上方问题"


def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s




def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


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


# 从首条用户消息派生简洁会话标题；斜杠命令不作为标题来源
def _derive_session_title(text: str, max_len: int = 20) -> str:
    cleaned = " ".join(text.split())
    if not cleaned or cleaned.startswith("/"):
        return ""
    return _preview(cleaned, max_len)


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "apply_patch": ("patch",),
        "checkpoint_list": (),
        "checkpoint_rewind": ("checkpoint_id",),
        "read_file": ("path",),
        "read_image": ("path",),
        "edit_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "glob": ("pattern", "path"),
        "git_diff": ("scope", "path"),
        "grep": ("pattern", "path", "glob"),
        "bash": ("command",),
        "web_fetch": ("url",),
        "web_search": ("query",),
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


_TOOL_ACTIONS: dict[str, tuple[str, str]] = {
    "apply_patch": ("正在应用补丁", "已应用补丁"),
    "background_cancel": ("正在停止后台任务", "已停止后台任务"),
    "background_list": ("正在查看后台任务", "已查看后台任务"),
    "background_run": ("正在启动后台命令", "已启动后台命令"),
    "background_status": ("正在检查后台任务", "已检查后台任务"),
    "bash": ("正在执行命令", "已执行命令"),
    "checkpoint_list": ("正在加载恢复点", "已加载恢复点"),
    "checkpoint_rewind": ("正在恢复检查点", "已恢复检查点"),
    "edit_file": ("正在修改", "已修改"),
    "git_diff": ("正在检查工作区改动", "已检查工作区改动"),
    "glob": ("正在搜索", "已搜索"),
    "grep": ("正在搜索", "已搜索"),
    "list_dir": ("正在查看目录", "已查看目录"),
    "memory_forget": ("正在删除项目记忆", "已删除项目记忆"),
    "memory_save": ("正在保存项目记忆", "已保存项目记忆"),
    "memory_search": ("正在搜索项目记忆", "已搜索项目记忆"),
    "note_save": ("正在保存笔记", "已保存笔记"),
    "read_file": ("正在读取", "已读取"),
    "read_image": ("正在读取图片", "已读取图片"),
    "spawn_agent": ("正在启动子代理", "已启动子代理"),
    "task_claim": ("正在认领任务", "已认领任务"),
    "task_create": ("正在创建任务", "已创建任务"),
    "task_list": ("正在加载任务", "已加载任务"),
    "task_update": ("正在更新任务", "已更新任务"),
    "web_fetch": ("正在抓取网页", "已抓取网页"),
    "web_search": ("正在搜索网页", "已搜索网页"),
    "write_file": ("正在写入", "已写入"),
}


# 把工具参数转换成接近自然语言的紧凑目标文本
def _tool_target(tool_name: str, params: dict[str, Any]) -> str:
    if tool_name == "File":
        action = str(params.get("action", ""))
        if action in {"search_name", "search_content"}:
            pattern = str(params.get("pattern", ""))
            path = str(params.get("path", "."))
            return _preview(f"{pattern} in {path}" if pattern else path, 110)
        if action == "patch":
            return "workspace patch"
        return _preview(str(params.get("path", ".")), 110)
    if tool_name == "Git":
        action = str(params.get("action", ""))
        if action == "show":
            revision = str(params.get("revision", ""))
            path = str(params.get("path", "."))
            return _preview(f"{revision} · {path}".strip(" ·"), 110)
        if action == "blame":
            return _preview(str(params.get("path", ".")), 110)
        return _preview(str(params.get("path", ".")), 110)
    if tool_name == "Run":
        action = str(params.get("action", ""))
        if action == "tests":
            return _preview(str(params.get("command", "tests")), 110)
        commands = params.get("commands", [])
        if isinstance(commands, list):
            return f"{len(commands)} verification gates"
        return "verification gates"
    if tool_name == "agent":
        action = str(params.get("action", "status"))
        if action == "start":
            return _preview(str(params.get("description", "worker")), 110)
        return _preview(str(params.get("worker_id", "workers")), 110)
    if tool_name == "Bash":
        action = str(params.get("action", "run"))
        if action == "run":
            return _preview(str(params.get("command", "")), 110)
        return _preview(str(params.get("job_id", "background job")), 110)
    if tool_name == "bash":
        return _preview(str(params.get("command", "")), 110)
    if tool_name in {"read_file", "edit_file", "write_file", "list_dir", "read_image"}:
        return _preview(str(params.get("path", ".")), 110)
    if tool_name in {"glob", "grep"}:
        pattern = str(params.get("pattern", ""))
        path = str(params.get("path", "."))
        target = f"{pattern} in {path}" if pattern else path
        return _preview(target, 110)
    if tool_name == "checkpoint_rewind":
        return _preview(str(params.get("checkpoint_id", "")), 110)
    if tool_name == "web_fetch":
        return _preview(str(params.get("url", "")), 110)
    if tool_name == "web_search":
        return _preview(str(params.get("query", "")), 110)
    if tool_name.startswith("background_"):
        value = params.get("command", params.get("job_id", ""))
        return _preview(str(value), 110)
    if tool_name.startswith("task_"):
        value = params.get("subject", params.get("task_id", ""))
        return _preview(str(value), 110)
    if tool_name == "spawn_agent":
        value = params.get("description", params.get("goal", ""))
        return _preview(str(value), 110)
    return _param_summary(tool_name, params, max_len=110)


# 生成工具运行中或完成后的自然动作文案
def _tool_action_text(
    tool_name: str,
    params: dict[str, Any],
    *,
    finished: bool,
) -> str:
    if tool_name == "File":
        action = str(params.get("action", ""))
        file_actions = {
            "read": ("正在读取", "已读取"),
            "list": ("正在查看目录", "已查看目录"),
            "search_name": ("正在按名称搜索", "已完成名称搜索"),
            "search_content": ("正在搜索内容", "已完成内容搜索"),
            "write": ("正在写入", "已写入"),
            "edit": ("正在修改", "已修改"),
            "patch": ("正在应用补丁", "已应用补丁"),
        }
        actions = file_actions.get(action, ("正在操作文件", "已完成文件操作"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "Git":
        action = str(params.get("action", ""))
        git_actions = {
            "status": ("正在检查 Git 状态", "已检查 Git 状态"),
            "diff": ("正在检查 Git 改动", "已检查 Git 改动"),
            "log": ("正在读取提交记录", "已读取提交记录"),
            "show": ("正在查看提交", "已查看提交"),
            "blame": ("正在追溯代码行", "已追溯代码行"),
        }
        actions = git_actions.get(action, ("正在读取 Git", "已读取 Git"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "Run":
        action = str(params.get("action", ""))
        run_actions = {
            "tests": ("正在运行测试", "已运行测试"),
            "verifiers": ("正在运行验证", "已运行验证"),
        }
        actions = run_actions.get(action, ("正在运行检查", "已运行检查"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "Bash":
        action = str(params.get("action", "run"))
        bash_actions = {
            "run": ("正在执行命令", "已执行命令"),
            "wait": ("正在等待后台任务", "已检查后台任务"),
            "interact": ("正在发送后台输入", "已发送后台输入"),
            "cancel": ("正在停止后台任务", "已停止后台任务"),
        }
        actions = bash_actions.get(action, ("正在操作命令", "已完成命令操作"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "agent":
        action = str(params.get("action", "status"))
        agent_actions = {
            "start": ("正在启动 Worker", "已启动 Worker"),
            "status": ("正在检查 Worker", "已检查 Worker"),
            "peek": ("正在查看 Worker 进度", "已查看 Worker 进度"),
            "wait": ("正在等待 Worker", "已等待 Worker"),
            "cancel": ("正在停止 Worker", "已停止 Worker"),
            "followup": ("正在发送 Worker 指令", "已发送 Worker 指令"),
        }
        actions = agent_actions.get(action, ("正在操作 Worker", "已完成 Worker 操作"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    actions = _TOOL_ACTIONS.get(
        tool_name,
        (f"正在执行 {tool_name}", f"已完成 {tool_name}"),
    )
    action = actions[1 if finished else 0]
    target = _tool_target(tool_name, params)
    return f"{action} {target}".rstrip()


class LLMStreamBlock(Widget):
    """流式思考时间线；最终回答自动退化为无标题的普通 Markdown。"""

    DEFAULT_CSS = """
    LLMStreamBlock { height: auto; padding: 0 2; color: $text; }
    LLMStreamBlock > .message-kind { color: #8e98a5; }
    LLMStreamBlock > .thought-body {
        padding: 0 1 0 2;
        margin-left: 1;
        border-left: solid #343b45;
        color: #aab2be;
    }
    LLMStreamBlock.answer > .message-kind { display: none; }
    LLMStreamBlock.answer > .thought-body {
        padding: 0;
        margin-left: 0;
        border-left: none;
        color: $text;
    }
    LLMStreamBlock.collapsed > .thought-body { display: none; }
    """

    # 初始化为空文本块
    def __init__(self) -> None:
        super().__init__()
        self._text = ""
        self._finalized = False
        self._kind = "working"

    # 根据流式与折叠状态生成思考区标题
    def _header(self) -> str:
        state = "深度思考" if self._finalized else "思考中"
        chevron = "›" if "collapsed" in self.classes else "⌄"
        return f"[#7f8996]◉[/#7f8996] [#8e98a5]{state}[/#8e98a5]  [dim]{chevron}[/dim]"

    # 根据流式状态挂载纯文本或支持屏幕选择的 Markdown 子组件
    def compose(self) -> ComposeResult:
        yield Static(self._header(), classes="message-kind")
        if self._finalized:
            if self._text.strip():
                yield Markdown(
                    self._text,
                    classes="assistant-response thought-body",
                )
            return
        yield Static(self._text, classes="stream-text thought-body", markup=False)

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
        self.set_class(kind in {"answer", "respond"}, "answer")
        if self.is_attached:
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

    # 点击思考标题时折叠或展开正文，最终回答不参与折叠
    def on_click(self, event: events.Click) -> None:
        if "answer" in self.classes:
            return
        try:
            header = self.query_one(".message-kind", Static)
        except NoMatches:
            return
        if event.widget is not header:
            return
        self.toggle_class("collapsed")
        header.update(self._header())


class ToolCallBlock(Widget):
    """Codex 风格工具行；默认紧凑，完成后可展开完整输入与结果。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 1; color: #9aa4b2; }
    ToolCallBlock > .summary { color: #aab2be; }
    ToolCallBlock > .detail {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 0 1 2;
        padding: 1 2;
        border: round #343b45;
        background: #15191e;
        color: #aab2be;
        overflow-x: auto;
        overflow-y: auto;
    }
    ToolCallBlock.expanded > .detail { display: block; }
    ToolCallBlock .detail-content { width: auto; height: auto; }
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
        with ScrollableContainer(classes="detail"):
            yield Static("", classes="detail-content", markup=False)

    # 生成自然动作、轻量状态图标和展开提示组成的单行摘要
    def _summary(self) -> str:
        if self._is_error:
            icon = "[bold red]×[/bold red]"
            attempted = _tool_action_text(self._tool_name, self._params, finished=False)
            action = f"执行失败：{attempted}"
        elif self._finished:
            icon = "[green]✓[/green]"
            action = _tool_action_text(self._tool_name, self._params, finished=True)
        else:
            icon = "[bold cyan]◌[/bold cyan]"
            action = _tool_action_text(self._tool_name, self._params, finished=False)
        chevron = ""
        if self._finished:
            chevron = (
                "  [#66717e]⌄[/#66717e]"
                if "expanded" in self.classes
                else "  [#66717e]›[/#66717e]"
            )
        return f"{icon} [#aab2be]{escape(action)}[/#aab2be]{chevron}"

    # 生成完整工具输入、结果与终态，供滚动详情面板按需展示
    def _detail_text(self) -> str:
        if self._tool_name in {"bash", "Bash"} and self._params.get(
            "action", "run"
        ) == "run":
            input_text = str(self._params.get("command", ""))
        else:
            input_text = self._params_full
        result_label = "Error" if self._is_error else "Response"
        status = "失败" if self._is_error else "成功"
        parts = [
            f"{self._tool_name}\n{input_text}",
            f"{result_label}\n{self._output.strip() or '(no output)'}",
            f"{'×' if self._is_error else '✓'} {status} · {self._elapsed_ms} ms",
        ]
        return "\n\n".join(parts)

    # 工具调用完成时先更新持久状态，再在已挂载时刷新可见控件
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        self.add_class("finished")
        self.set_class(is_error, "failed")
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 仅允许已完成工具展开详情并同步摘要与完整内容
    def set_expanded(self, expanded: bool) -> None:
        if not self._finished:
            self.remove_class("expanded")
            return
        if expanded:
            self.query_one(".detail-content", Static).update(self._detail_text())
            self.add_class("expanded")
        else:
            self.remove_class("expanded")
        self.query_one(".summary", Static).update(self._summary())

    # 点击已完成工具行时切换完整详情
    def on_click(self) -> None:
        self.set_expanded("expanded" not in self.classes)


class ToolStepGroup(Widget):
    """把同一 Agent step 的工具调用合并为可折叠的步骤时间线。"""

    DEFAULT_CSS = """
    ToolStepGroup { height: auto; padding: 0 2; }
    ToolStepGroup > .step-header { color: #8e98a5; }
    ToolStepGroup > .step-body {
        height: auto;
        margin-left: 1;
        padding-left: 1;
        border-left: solid #343b45;
    }
    ToolStepGroup.collapsed > .step-body { display: none; }
    """

    # 初始化空步骤组并记录 step 编号
    def __init__(self, step: int) -> None:
        super().__init__()
        self.step = step
        self._blocks: list[ToolCallBlock] = []

    # 按工具种类聚合同一步的自然动作摘要
    def _action_summary(self) -> str:
        names = [block._tool_name for block in self._blocks]
        labels: list[str] = []
        categories = (
            ({"bash", "background_run"}, "运行了命令", "运行了 {count} 条命令"),
            ({"read_file", "list_dir"}, "读取了文件", "读取了 {count} 个位置"),
            ({"grep", "glob", "memory_search"}, "搜索了内容", "进行了 {count} 次搜索"),
            ({"web_search", "web_fetch"}, "搜索了网页", "搜索了 {count} 次网页"),
            ({"edit_file", "write_file", "apply_patch"}, "编辑了文件", "编辑了 {count} 个文件"),
        )
        categorized: set[str] = set()
        for tool_names, singular, plural in categories:
            count = sum(name in tool_names for name in names)
            if count:
                labels.append(singular if count == 1 else plural.format(count=count))
                categorized.update(name for name in names if name in tool_names)
        for name in names:
            if name not in categorized:
                label = f"使用了 {name}"
                if label not in labels:
                    labels.append(label)
        return " · ".join(labels) or "执行工具"

    # 根据聚合动作和折叠状态生成步骤标题
    def _header(self) -> str:
        chevron = "›" if "collapsed" in self.classes else "⌄"
        return (
            "[#7f8996]▣[/#7f8996] "
            f"[#8e98a5]{escape(self._action_summary())}[/#8e98a5]  "
            f"[dim]{chevron}[/dim]"
        )

    # 挂载标题和当前已收集的工具调用
    def compose(self) -> ComposeResult:
        yield Static(self._header(), classes="step-header")
        yield Vertical(*self._blocks, classes="step-body")

    # 向步骤组追加一个工具块并刷新计数
    def add_tool(self, block: ToolCallBlock) -> None:
        self._blocks.append(block)
        if not self.children:
            return
        self.query_one(".step-body", Vertical).mount(block)
        self.query_one(".step-header", Static).update(self._header())

    # 点击步骤标题时折叠或展开整个工具列表
    def on_click(self, event: events.Click) -> None:
        try:
            header = self.query_one(".step-header", Static)
        except NoMatches:
            return
        if event.widget is not header:
            return
        self.toggle_class("collapsed")
        header.update(self._header())


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


class UserQuestionSelect(Static):
    can_focus = True

    DEFAULT_CSS = """
    UserQuestionSelect {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    UserQuestionSelect:focus { border: solid #72c7d4; }
    """

    class Answered(Message):
        # 初始化结构化问题回答消息，answer 为 None 时改用输入框自由回答
        def __init__(
            self,
            select: UserQuestionSelect,
            answer: str | None,
        ) -> None:
            self.select = select
            self.answer = answer
            super().__init__()

    # 初始化结构化问题选择器并添加自由回答入口
    def __init__(
        self,
        question_id: str,
        question: str,
        header: str,
        options: list[str],
        multi_select: bool,
    ) -> None:
        super().__init__("")
        self.question_id = question_id
        self._question = question
        self._header = header
        self._options = options
        self._choices = [*options, "输入自定义答案"]
        self._multi_select = multi_select
        self._selected: set[int] = set()
        self._cursor = 0

    # 挂载后渲染问题和可选答案并取得焦点
    def on_mount(self) -> None:
        self.border_title = f" {self._header} "
        self.border_subtitle = (
            " ↑↓ move   Space toggle   Enter confirm "
            if self._multi_select
            else " ↑↓ move   Enter select "
        )
        self.update(self._render_ui())
        self.focus()

    # 渲染结构化问题、当前光标和多选状态
    def _render_ui(self) -> str:
        lines = [f"[bold]{escape(self._question)}[/bold]"]
        for index, choice in enumerate(self._choices):
            cursor = "[bold #72c7d4]>[/bold #72c7d4]" if index == self._cursor else " "
            checked = (
                "[cyan][x][/cyan]"
                if index in self._selected
                else ("[dim][ ][/dim]" if self._multi_select else "")
            )
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{cursor} {checked} [{style}]{escape(choice)}[/{style}]"
            )
        return "\n".join(lines)

    # 提交当前选择，多选时按原选项顺序合并答案
    def _submit(self) -> None:
        custom_index = len(self._choices) - 1
        if self._cursor == custom_index:
            self.post_message(self.Answered(self, None))
            return
        if self._multi_select:
            self._selected.add(self._cursor)
            answers = [
                self._choices[index]
                for index in sorted(self._selected)
                if index < custom_index
            ]
            if answers:
                self.post_message(self.Answered(self, ", ".join(answers)))
            return
        self.post_message(self.Answered(self, self._choices[self._cursor]))

    # 处理结构化问题的键盘导航、多选和确认
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._choices)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._choices)
            self.update(self._render_ui())
        elif event.key == "space" and self._multi_select:
            event.stop()
            if self._cursor < len(self._choices) - 1:
                if self._cursor in self._selected:
                    self._selected.remove(self._cursor)
                else:
                    self._selected.add(self._cursor)
                self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            self._submit()
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Answered(self, None))


class CheckpointPicker(Static):
    can_focus = True

    DEFAULT_CSS = """
    CheckpointPicker {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #8c6d3f;
        border-title-color: #d8a65b;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    CheckpointPicker:focus { border: solid #d8a65b; }
    """

    class Selected(Message):
        # 初始化 checkpoint 选择消息
        def __init__(self, picker: CheckpointPicker, checkpoint_id: str) -> None:
            self.picker = picker
            self.checkpoint_id = checkpoint_id
            super().__init__()

    class Dismissed(Message):
        # 初始化 checkpoint 选择器关闭消息
        def __init__(self, picker: CheckpointPicker) -> None:
            self.picker = picker
            super().__init__()

    # 初始化 checkpoint 选择器，仅接收可恢复状态的条目
    def __init__(self, checkpoints: list[dict[str, Any]]) -> None:
        super().__init__("")
        self._checkpoints = checkpoints
        self._cursor = 0

    # 挂载后渲染安全恢复点并取得焦点
    def on_mount(self) -> None:
        self.border_title = " Rewind "
        self.border_subtitle = " ↑↓ move   Enter restore   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 渲染 checkpoint 标签、文件和创建时间
    def _render_ui(self) -> str:
        lines = ["[bold yellow]选择要恢复的 checkpoint[/bold yellow]"]
        for index, checkpoint in enumerate(self._checkpoints):
            marker = "[bold #d8a65b]>[/bold #d8a65b]" if index == self._cursor else " "
            label = escape(str(checkpoint.get("label", "checkpoint")))
            paths = ", ".join(str(path) for path in checkpoint.get("paths", []))
            created = escape(str(checkpoint.get("created_at", ""))[:19])
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{marker} [{style}]{label}[/{style}]"
                f"  [dim]{escape(paths)}  {created}[/dim]"
            )
        lines.append("[dim]恢复会拒绝覆盖 checkpoint 之后再次修改的文件[/dim]")
        return "\n".join(lines)

    # 处理 checkpoint 选择器的键盘导航和确认
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._checkpoints)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._checkpoints)
            self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            checkpoint_id = str(
                self._checkpoints[self._cursor].get("checkpoint_id", "")
            )
            self.post_message(self.Selected(self, checkpoint_id))
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
            "ctrl+end",
            "scroll_log_end",
            "跳回日志底部",
            show=False,
            priority=True,
        ),
        Binding(
            "tab",
            "cycle_runtime_mode",
            "cycle runtime mode",
            show=False,
            priority=True,
        ),
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
    Static.user-turn {
        color: $text;
        padding: 1 2 1 18;
        text-align: right;
    }
    Static.run-err { color: red; padding: 0 2 1 2; }
    Static.log-line { padding: 0 2; }
    Static.permission-pending { display: none; }
    Markdown.history-assistant { color: $text; }
    """

    _BANNER = (
        "[bold cyan]"
        " ██████╗ ██████╗ ██████╗ ███████╗    ██████╗  ██████╗  ██████╗ ██╗  ██╗\n"
        "██╔════╝██╔═══██╗██╔══██╗██╔════╝    ██╔══██╗██╔═══██╗██╔═══██╗██║ ██╔╝\n"
        "██║     ██║   ██╗██║  ██║█████╗      ██████╔╝██║   ██╗██║   ██║█████╔╝ \n"
        "██║     ██║   ██║██║  ██║██╔══╝      ██╔══██╗██║   ██╗██║   ██║██╔═██╗ \n"
        "╚██████╗╚██████╔╝██████╔╝███████╗    ██║  ██║╚██████╔╝╚██████╔╝██║  ██╗\n"
        " ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝    ╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝"
        "[/bold cyan]\n"
        "[dim]  输入消息开始对话  ·  /help 查看键位与命令  ·  拖选后 Ctrl+C 复制"
        "  ·  Ctrl+Q 退出[/dim]"
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
        route: str = "",
        route_store: RouteStore | None = None,
        credential_store: CredentialStore | None = None,
        provider_doctor: ProviderDoctor | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._replay_run_id = replay_run_id
        self._resume_session_id = resume_session_id
        self._auth_token = auth_token
        self._provider = provider
        self._route = route
        self._model = model
        self._models = models or ([model] if model else [])
        self._route_store = route_store or RouteStore()
        self._credential_store = credential_store or CredentialStore()
        self._provider_doctor = provider_doctor or ProviderDoctor()
        self._config_provider: ProviderPreset | None = None
        self._pending_config_key: str | None = None
        self._discovered_config_models: tuple[str, ...] = ()
        self._history_loaded = False
        self._client: SocketClient | None = None
        self._current_llm: LLMStreamBlock | None = None
        self._pending_tool_blocks: dict[str, ToolCallBlock] = {}
        self._current_steps: dict[str, int] = {}
        self._tool_step_groups: dict[tuple[str, int], ToolStepGroup] = {}
        self._pending_permission_blocks: dict[str, PermissionBlock] = {}
        self._session_id: str | None = None
        self._active_run_id: str | None = None
        self._cancel_requested = False
        self._cancel_armed = False
        self._busy = False
        self._last_context_pct: float = 0.0
        self._last_assistant_text = ""
        self._header_state = "connecting"
        self._session_title = ""
        self._titled = False
        self._first_user_text = ""
        # 会话级成本累计：总额、按模型分解、缓存节省、无价模型标记
        self._cost_total: float = 0.0
        self._cost_by_model: dict[str, float] = {}
        self._tokens_by_model: dict[str, dict[str, int]] = {}
        self._cache_saved_total: float = 0.0
        self._unpriced_models: set[str] = set()
        self._pricing_overrides = load_pricing_overrides()
        self._authority_preset = "ask"
        self._input_runtime_mode = RuntimeMode.ACT
        self._workspace_trust = WorkspaceTrust.UNTRUSTED
        self._sandbox: dict[str, Any] = {
            "available": False,
            "kind": "none",
            "reason": "sandbox capability has not been detected",
        }
        self._plan_review_pending = False
        self._plan_session_id: str | None = None
        self._plan_request = ""
        self._pending_question_id: str | None = None
        self._answering_question = False
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
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.set_history(_load_input_history())
        prompt.disabled = True
        prompt.border_title = _PROMPT_CONNECTING
        self.run_worker(self._socket_loop(), exclusive=True, name="socket")

    # 构建斜杠命令候选列表：内建命令 + 所有已注册 skill
    def _build_slash_items(self) -> list[tuple[str, str]]:
        items: list[tuple[str, str]] = [
            ("help", "显示键位与全部命令"),
            ("sessions", "打开会话选择器（输入即过滤）"),
            ("new", "新建会话"),
            ("rename", "重命名当前会话：/rename <标题>"),
            ("fork", "复制当前会话为分支：/fork [标题]"),
            ("export", "导出当前会话：/export [md|json]"),
            ("delete", "删除当前会话（需 --yes 确认）"),
            ("provider", "查看或切换 Provider route"),
            ("model", "查看或切换模型"),
            ("doctor", "诊断活动 Provider route"),
            ("config", "更换 LLM API、模型或密钥"),
            ("compact", "手动压缩上下文"),
            ("copy", "复制上一条回复"),
            ("plan", "只读规划并审阅后再实施：/plan [任务]"),
            ("mode", "查看或切换工作模式：plan|act|operate"),
            ("permissions", "查看或切换权限模式"),
            ("trust", "查看或授予/撤销工作区信任"),
            ("sandbox", "查看 OS 隔离能力（仅探测）"),
            ("tasks", "查看最近一次 run 的任务"),
            ("workers", "查看全部持久 Worker 与 Fleet"),
            ("workflow", "查看、启动或检查 workflow"),
            ("diff", "查看工作区改动"),
            ("rewind", "从安全恢复点回滚文件"),
            ("context", "查看上下文占用与用量"),
            ("cost", "查看本会话成本分解与缓存节省"),
            ("turn", "检查 route、用量、审批与收据"),
            ("skills", "列出、查看、安装或删除 skills"),
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

    # 在询问、自动接受修改和全自动执行之间循环权限姿态
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

    # 在 Act、Operate 和 Plan 之间循环工作模式
    async def action_cycle_runtime_mode(self) -> None:
        try:
            popup = self.query_one(SlashCompleteWidget)
        except NoMatches:
            popup = None
        if popup is not None and popup.has_selection():
            popup.select_current()
            return
        if self._busy or self._plan_review_pending:
            self._append(
                Static(
                    "[yellow]当前 run 或计划审阅完成后再切换工作模式[/yellow]",
                    classes="log-line",
                )
            )
            return
        index = (_MODE_CYCLE.index(self._input_runtime_mode) + 1) % len(_MODE_CYCLE)
        await self._select_runtime_mode(_MODE_CYCLE[index])

    # 输入框在没有斜杠补全时用 Tab 请求切换工作模式
    async def on_chat_text_area_cycle_mode(self, _message: ChatTextArea.CycleMode) -> None:
        await self.action_cycle_runtime_mode()

    # 退出只断开界面，session 保留在 Core 中以便下次 resume
    async def action_quit(self) -> None:
        self.exit()

    # 将日志视图跳回底部，恢复自动跟随
    def action_scroll_log_end(self) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        log_view.scroll_end(animate=False)

    async def action_cancel_run(self) -> None:
        if (
            self._client is None
            or self._active_run_id is None
            or not self._busy
            or self._cancel_requested
        ):
            return
        if not self._cancel_armed:
            self._cancel_armed = True
            self._append(
                Static(
                    "[yellow]再次 Ctrl+C 确认取消当前任务 · Ctrl+Q 退出 TUI[/yellow]",
                    classes="log-line",
                )
            )
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

    # 在日志中渲染键位说明和全部内建斜杠命令，作为 TUI 内的帮助面板
    def _show_help(self) -> None:
        keys = [
            ("Enter", "发送消息；Shift/Alt+Enter 或 Ctrl+J 换行"),
            ("↑ / ↓", "空输入时回溯输入历史，再按 ↑ 更早、↓ 更新"),
            ("Tab", "循环工作模式 Act → Operate → Plan"),
            ("Shift+Tab", "循环权限姿态 ask → accept edits → full access"),
            ("Ctrl+C", "复制拖选文本；无选择时第一次按下提示、再按一次取消当前任务"),
            ("Ctrl+Shift+C", "复制选择；无选择时复制上一条回复"),
            ("Ctrl+End", "日志跳回底部（上滚暂停自动跟随后恢复）"),
            ("Ctrl+Q", "退出 TUI（会话保留，可 resume）"),
        ]
        lines = ["[bold cyan]键位[/bold cyan]"]
        for key, desc in keys:
            lines.append(f"  [bold]{key}[/bold]  {escape(desc)}")
        lines.append("[bold cyan]命令[/bold cyan]")
        for name, desc in self._slash_items:
            lines.append(f"  [cyan]/{name}[/cyan]  [dim]{escape(desc)}[/dim]")
        lines.append(
            "[dim]输入 / 后可用 ↑↓ 浏览、Tab 补全；"
            "未匹配内建命令的 /名称 会作为 skill 发送给 Agent[/dim]"
        )
        self._append(Static("\n".join(lines), classes="log-line"))

    # 在 TUI 本地执行 /skills list/show/install/remove/audit，并要求变更操作显式确认
    def _handle_skills_command(self, content: str) -> None:
        try:
            parts = shlex.split(content)
        except ValueError as exc:
            self._append(
                Static(
                    f"[red]skills 参数错误：{escape(str(exc))}[/red]",
                    classes="log-line",
                )
            )
            return
        action = parts[1] if len(parts) > 1 else "list"
        args = parts[2:]
        manager = SkillManager(Path.cwd())
        try:
            if action == "list":
                skills = manager.list_all()
                lines = [
                    f"{skill.name}  {skill.scope}  {skill.trust}  {skill.integrity}"
                    for skill in skills
                ]
                self._append(
                    Static(
                        "[bold cyan]Skills[/bold cyan]\n"
                        + escape("\n".join(lines) or "No skills."),
                        classes="log-line",
                    )
                )
                return
            if action == "show" and args:
                skill = manager.show(args[0])
                if skill is None:
                    raise SkillManagerError(f"skill not found: {args[0]}")
                payload = skill.model_dump(
                    mode="json",
                    exclude={"system_prompt_template"},
                )
                self._append(
                    Static(
                        "[bold cyan]Skill manifest[/bold cyan]\n"
                        + escape(json.dumps(payload, ensure_ascii=False, indent=2)),
                        classes="log-line",
                    )
                )
                return
            scope: InstallScope = "user" if "--user" in args else "project"
            if "--scope" in args:
                scope_index = args.index("--scope")
                if scope_index + 1 >= len(args) or args[scope_index + 1] not in {"user", "project"}:
                    raise SkillManagerError("--scope must be project or user")
                scope = cast(InstallScope, args[scope_index + 1])
            if action == "install" and args:
                installed = manager.install(
                    args[0],
                    scope=scope,
                    trust="trusted" if "--trust" in args else "untrusted",
                    confirmed="--yes" in args,
                    overwrite="--force" in args,
                )
                self._slash_items = self._build_slash_items()
                self._append(
                    Static(
                        f"[green]已安装 skill {escape(installed.name)}[/green]",
                        classes="log-line",
                    )
                )
                return
            if action == "remove" and args:
                manager.remove(
                    args[0],
                    scope=scope,
                    confirmed="--yes" in args,
                )
                self._slash_items = self._build_slash_items()
                self._append(
                    Static(
                        f"[green]已删除 {scope} skill {escape(args[0])}[/green]",
                        classes="log-line",
                    )
                )
                return
            if action == "audit":
                records = manager.audit()
                lines = [
                    f"{record.name}  {record.scope}  {record.trust}  {record.integrity}  "
                    f"{record.digest[:19]}"
                    for record in records
                ]
                self._append(
                    Static(
                        "[bold cyan]Skill audit[/bold cyan]\n"
                        + escape("\n".join(lines) or "No skills."),
                        classes="log-line",
                    )
                )
                return
            raise SkillManagerError(
                "用法：/skills list|show <name>|install <path> [--scope user|project] "
                "[--trust] [--yes]|remove <name> [--scope user|project] --yes|audit"
            )
        except SkillConfirmationRequired as exc:
            preview = json.dumps(exc.preview.model_dump(mode="json"), ensure_ascii=False, indent=2)
            self._append(
                Static(
                    "[bold yellow]安装预览（尚未写入）[/bold yellow]\n"
                    + escape(preview)
                    + "\n[dim]确认后在相同命令末尾添加 --yes[/dim]",
                    classes="log-line",
                )
            )
        except (SkillManagerError, OSError) as exc:
            self._append(
                Static(f"[red]skills 错误：{escape(str(exc))}[/red]", classes="log-line")
            )

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
        event.text_area.record_history(content)
        if self._pending_question_id is not None and self._answering_question:
            event.text_area.text = ""
            event.text_area.disabled = True
            self._append(
                Static(
                    f"[bold cyan]answer >[/bold cyan] {escape(content)}",
                    classes="user-turn",
                )
            )
            self.run_worker(
                self._do_answer_question(self._pending_question_id, content),
                name="answer_question",
                exclusive=False,
            )
            return
        if self._busy:
            event.text_area.text = ""
            if self._client is None or self._active_run_id is None:
                self._append(
                    Static("[yellow]run 正在启动，请稍后再输入纠偏[/yellow]", classes="log-line")
                )
                return
            self._append(
                Static(
                    f"[bold magenta]steer >[/bold magenta] {escape(content)}",
                    classes="user-turn",
                )
            )
            self.run_worker(
                self._do_steer(self._active_run_id, content),
                name="steer_run",
                exclusive=False,
            )
            return
        if content == "/skills" or content.startswith("/skills "):
            event.text_area.text = ""
            self._handle_skills_command(content)
            return
        if content == "/help":
            event.text_area.text = ""
            self._show_help()
            return
        if content == "/permissions":
            event.text_area.text = ""
            event.text_area.disabled = True
            event.text_area.border_title = "选择权限模式"
            try:
                self.query_one(PermissionModePicker).remove()
            except NoMatches:
                pass
            self.mount(
                PermissionModePicker(self._authority_preset),
                before="#prompt",
            )
            return
        if content.startswith("/permissions "):
            event.text_area.text = ""
            requested = content.removeprefix("/permissions ").strip().replace("-", "_")
            aliases = {
                "ask": "ask",
                "auto_review": "accept_edits",
                "accept_edits": "accept_edits",
                "full_access": "full_access",
            }
            preset = aliases.get(requested)
            if preset is None:
                self._append(
                    Static(
                        "[yellow]用法：/permissions ask|auto-review|full-access[/yellow]",
                        classes="log-line",
                    )
                )
            else:
                await self._select_authority_preset(preset)
            return
        if content == "/mode":
            event.text_area.text = ""
            self._append(
                Static(
                    f"[bold cyan]工作模式[/bold cyan]  {self._input_runtime_mode.value}"
                    "  [dim]用法：/mode plan|act|operate[/dim]",
                    classes="log-line",
                )
            )
            return
        if content.startswith("/mode "):
            event.text_area.text = ""
            raw_mode = content.removeprefix("/mode ").strip()
            try:
                mode = RuntimeMode(raw_mode)
            except ValueError:
                self._append(
                    Static(
                        "[yellow]用法：/mode plan|act|operate[/yellow]",
                        classes="log-line",
                    )
                )
            else:
                await self._select_runtime_mode(mode)
            return
        if content == "/trust" or content.startswith("/trust "):
            event.text_area.text = ""
            action = content.removeprefix("/trust").strip() or "status"
            if action == "status":
                self._show_trust_status()
            elif action in {"grant", "revoke"}:
                trust = (
                    WorkspaceTrust.TRUSTED
                    if action == "grant"
                    else WorkspaceTrust.UNTRUSTED
                )
                await self._set_workspace_trust(trust)
            else:
                self._append(
                    Static(
                        "[yellow]用法：/trust status|grant|revoke[/yellow]",
                        classes="log-line",
                    )
                )
            return
        if content == "/sandbox" or content == "/sandbox status":
            event.text_area.text = ""
            self._show_sandbox_status()
            return
        if content == "/plan":
            event.text_area.text = ""
            await self._select_runtime_mode(RuntimeMode.PLAN, announce=False)
            return
        requested_mode = self._input_runtime_mode
        if content.startswith("/plan "):
            content = content.removeprefix("/plan ").strip()
            if not content:
                event.text_area.text = ""
                event.text_area.border_title = _PROMPT_PLAN
                self._input_runtime_mode = RuntimeMode.PLAN
                return
            requested_mode = RuntimeMode.PLAN
        if content == "/sessions":
            event.text_area.text = ""
            if self._client is not None and not self._busy:
                event.text_area.disabled = True
                event.text_area.border_title = "正在加载会话"
                self.run_worker(self._show_session_picker(), name="session_picker", exclusive=False)
            return
        if content == "/new":
            event.text_area.text = ""
            if self._client is not None and not self._busy:
                event.text_area.disabled = True
                event.text_area.border_title = "正在创建会话"
                self.run_worker(
                    self._create_and_switch_session(),
                    name="new_session",
                    exclusive=False,
                )
            return
        if content == "/rename" or content.startswith("/rename "):
            event.text_area.text = ""
            title = content.removeprefix("/rename").strip()
            if not title:
                self._append(
                    Static("[yellow]用法：/rename <新标题>[/yellow]", classes="log-line")
                )
            elif self._client is not None and self._session_id is not None and not self._busy:
                event.text_area.disabled = True
                event.text_area.border_title = "正在重命名会话"
                self.run_worker(
                    self._do_rename_session(title),
                    name="rename_session",
                    exclusive=False,
                )
            return
        if content == "/fork" or content.startswith("/fork "):
            event.text_area.text = ""
            if self._client is None or self._session_id is None or self._busy:
                self._append(
                    Static("[yellow]Core 未连接或任务运行中，稍后再试[/yellow]", classes="log-line")
                )
                return
            title = content.removeprefix("/fork").strip()
            event.text_area.disabled = True
            event.text_area.border_title = "正在复制会话"
            self.run_worker(
                self._do_fork_session(title),
                name="fork_session",
                exclusive=False,
            )
            return
        if content == "/export" or content.startswith("/export "):
            event.text_area.text = ""
            if self._client is None or self._session_id is None:
                self._append(Static("[yellow]Core 未连接[/yellow]", classes="log-line"))
                return
            fmt = content.removeprefix("/export").strip().lower()
            if fmt not in {"", "md", "json", "markdown"}:
                self._append(
                    Static("[yellow]用法：/export [md|json][/yellow]", classes="log-line")
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "正在导出会话"
            self.run_worker(
                self._do_export_session(fmt),
                name="export_session",
                exclusive=False,
            )
            return
        if content == "/delete" or content.startswith("/delete "):
            event.text_area.text = ""
            if self._client is None or self._session_id is None or self._busy:
                self._append(
                    Static("[yellow]Core 未连接或任务运行中，稍后再试[/yellow]", classes="log-line")
                )
                return
            if "--yes" not in content:
                self._append(
                    Static(
                        f"[yellow]将删除当前会话 {escape(self._session_id)} 及其全部历史，"
                        "确认请输入 /delete --yes[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "正在删除会话"
            self.run_worker(
                self._do_delete_session(),
                name="delete_session",
                exclusive=False,
            )
            return
        workflow_command = content == "/workflow" or content.startswith("/workflow ")
        turn_command = content == "/turn" or content.startswith("/turn ")
        if (
            content in {"/tasks", "/workers", "/diff", "/rewind", "/context"}
            or workflow_command
            or turn_command
        ):
            event.text_area.text = ""
            if self._client is None or (
                self._session_id is None and not workflow_command
            ):
                self._append(
                    Static("[yellow]Core 未连接[/yellow]", classes="log-line")
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = f"正在加载 {content}"
            workers = {
                "/tasks": self._show_tasks,
                "/workers": self._show_workers,
                "/diff": self._show_diff,
                "/rewind": self._show_rewind_picker,
                "/context": self._show_context,
            }
            if workflow_command:
                workflow_arg = content.removeprefix("/workflow").strip()
                if workflow_arg.startswith("start "):
                    workflow_path = workflow_arg.removeprefix("start ").strip()
                    self.run_worker(
                        self._start_workflow_file(workflow_path),
                        name="start_workflow",
                        exclusive=False,
                    )
                    return
                self.run_worker(
                    self._show_workflow(workflow_arg),
                    name="view_workflow",
                    exclusive=False,
                )
                return
            if turn_command:
                turn_id = content.removeprefix("/turn").strip()
                self.run_worker(
                    self._show_turn(turn_id),
                    name="view_turn",
                    exclusive=False,
                )
                return
            self.run_worker(
                workers[content](),
                name=f"view_{content[1:]}",
                exclusive=False,
            )
            return
        if content == "/provider":
            event.text_area.text = ""
            self._show_provider_routes()
            return
        if content.startswith("/provider "):
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]当前任务运行中，结束后再切换 Provider"
                        "（Ctrl+C 可取消任务）[/yellow]",
                        classes="log-line",
                    )
                )
                return
            self._select_provider_route(content.removeprefix("/provider ").strip())
            return
        if content == "/doctor":
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]当前任务运行中，结束后再运行诊断（Ctrl+C 可取消任务）[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "正在诊断 Provider"
            self.run_worker(
                self._show_provider_doctor(),
                name="provider_doctor",
                exclusive=False,
            )
            return
        if content == "/model":
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]当前任务运行中，结束后再切换模型（Ctrl+C 可取消任务）[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "选择模型"
            self.mount(ModelPicker(self._models, self._model), before="#prompt")
            return
        if content.startswith("/model "):
            event.text_area.text = ""
            if self._busy:
                self._append(
                    Static(
                        "[yellow]当前任务运行中，结束后再切换模型（Ctrl+C 可取消任务）[/yellow]",
                        classes="log-line",
                    )
                )
                return
            selected = content.removeprefix("/model ").strip()
            if selected.startswith("add "):
                selected = selected.removeprefix("add ").strip()
            if selected:
                self._select_route_model(selected)
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
                        "[yellow]当前任务运行中，结束后再修改 LLM 配置"
                        "（Ctrl+C 可取消任务）[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.disabled = True
            event.text_area.border_title = "选择 API 平台"
            self.mount(
                ProviderPicker(PROVIDER_PRESETS, self._provider),
                before="#prompt",
            )
            return
        if content == "/copy":
            event.text_area.text = ""
            self._copy_last_response()
            return
        if content == "/cost":
            event.text_area.text = ""
            self._show_cost_breakdown()
            return
        # 检测 /compact 指令
        if content == "/compact":
            event.text_area.text = ""
            if self._client is not None and self._session_id is not None and not self._busy:
                self.run_worker(self._do_compact(), name="compact", exclusive=False)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(
                Static("[yellow]Agent 忙碌或未连接，请稍后再试[/yellow]", classes="log-line")
            )
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
        self._cancel_armed = False
        prompt.text = ""
        prompt.disabled = False
        prompt.read_only = False
        prompt.border_title = (
            _PROMPT_PLANNING
            if runtime_mode == RuntimeMode.PLAN
            else _PROMPT_RUNNING
        )
        shown = visible_content if visible_content is not None else content
        if visible_content is None and not self._first_user_text:
            self._first_user_text = content
        visible = (
            f"[bold cyan]Plan[/bold cyan]  {escape(shown)}"
            if runtime_mode == RuntimeMode.PLAN
            else escape(shown)
        )
        self._append(Static(visible, classes="user-turn"))
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
                prompt.border_title = _PROMPT_PLAN
            else:
                prompt.border_title = _PROMPT_READY
            prompt.focus()

    # 将 Core 返回的四维 authority 快照映射到 TUI 状态
    def _apply_authority_snapshot(self, snapshot: dict[str, Any]) -> None:
        mode = RuntimeMode(str(snapshot.get("mode", RuntimeMode.ACT.value)))
        profile = AuthorityProfile(
            str(snapshot.get("profile", AuthorityProfile.ASK.value))
        )
        if profile == AuthorityProfile.FULL_ACCESS:
            preset = "full_access"
        elif profile == AuthorityProfile.AUTO_REVIEW:
            preset = "accept_edits"
        else:
            preset = "ask"
        self._authority_preset = preset
        self._input_runtime_mode = mode
        self._workspace_trust = WorkspaceTrust(
            str(snapshot.get("workspace_trust", WorkspaceTrust.UNTRUSTED.value))
        )
        sandbox = snapshot.get("sandbox")
        if isinstance(sandbox, dict):
            self._sandbox = dict(sandbox)

    # 读取当前会话的 authority，保证续接和切换会话后状态指示真实
    async def _refresh_authority(self) -> None:
        if self._client is None or self._session_id is None:
            return
        result = await self._client.send_command(
            "session.get_authority",
            {"session_id": self._session_id},
        )
        self._apply_authority_snapshot(dict(result.get("snapshot", {})))

    # 持久化并应用用户选择的权限姿态，不改变工作模式
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
        _name, profile, label, _detail = selected
        try:
            if self._client is not None and self._session_id is not None:
                result = await self._client.send_command(
                    "session.set_authority",
                    {
                        "session_id": self._session_id,
                        "profile": profile.value,
                    },
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._authority_preset = preset
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(
                Static(f"[red]permission mode error: {escape(str(exc))}[/red]", classes="log-line")
            )
            self._restore_ready_prompt()
            return
        self._restore_ready_prompt()
        self._update_header(
            "plan" if self._input_runtime_mode == RuntimeMode.PLAN else "ready"
        )
        if announce:
            self._append(
                Static(
                    f"[bold cyan]权限模式[/bold cyan]  {escape(label)}",
                    classes="log-line",
                )
            )

    # 持久化并应用工作模式，不改变权限姿态
    async def _select_runtime_mode(
        self,
        mode: RuntimeMode,
        *,
        announce: bool = True,
    ) -> None:
        try:
            if self._client is not None and self._session_id is not None:
                result = await self._client.send_command(
                    "session.set_authority",
                    {"session_id": self._session_id, "mode": mode.value},
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._input_runtime_mode = mode
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(
                Static(f"[red]runtime mode error: {escape(str(exc))}[/red]", classes="log-line")
            )
            self._restore_ready_prompt()
            return
        self._restore_ready_prompt()
        self._update_header("plan" if mode == RuntimeMode.PLAN else "ready")
        if announce:
            self._append(
                Static(
                    f"[bold cyan]工作模式[/bold cyan]  {escape(mode.value)}",
                    classes="log-line",
                )
            )

    # 更新工作区信任状态并保留 mode、authority 和 sandbox
    async def _set_workspace_trust(self, trust: WorkspaceTrust) -> None:
        try:
            if self._client is not None and self._session_id is not None:
                result = await self._client.send_command(
                    "session.set_authority",
                    {
                        "session_id": self._session_id,
                        "workspace_trust": trust.value,
                    },
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._workspace_trust = trust
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(
                Static(f"[red]workspace trust error: {escape(str(exc))}[/red]", classes="log-line")
            )
            return
        self._update_header("plan" if self._input_runtime_mode == RuntimeMode.PLAN else "ready")
        self._show_trust_status()

    # 在 transcript 中显示当前工作区信任状态
    def _show_trust_status(self) -> None:
        self._append(
            Static(
                f"[bold cyan]Workspace trust[/bold cyan]  {self._workspace_trust.value}",
                classes="log-line",
            )
        )

    # 在 transcript 中如实显示操作系统隔离能力
    def _show_sandbox_status(self) -> None:
        available = bool(self._sandbox.get("available", False))
        kind = escape(str(self._sandbox.get("kind", "none")))
        reason = escape(str(self._sandbox.get("reason", "unavailable")))
        state = "detected" if available else "not detected"
        color = "green" if available else "yellow"
        self._append(
            Static(
                f"[bold cyan]Sandbox[/bold cyan]  [{color}]{state}[/{color}]"
                f"  [dim]{kind}: {reason}[/dim]\n"
                "[dim]仅为能力探测（advisory）：当前不会实际隔离命令执行，"
                "安全依赖审批链与工作区边界。[/dim]",
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

    # 加载并展示最近一次 run 的任务状态
    async def _show_tasks(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "session.tasks",
                {"session_id": self._session_id},
            )
            tasks = list(result.get("tasks", []))
            if not tasks:
                body = "[dim]当前会话最近一次 run 没有任务。[/dim]"
            else:
                markers = {
                    "pending": "[ ]",
                    "ready": "[ ]",
                    "running": "[>]",
                    "blocked": "[-]",
                    "completed": "[x]",
                    "failed": "[!]",
                    "cancelled": "[-]",
                    "in_progress": "[>]",
                }
                lines = ["[bold cyan]Tasks[/bold cyan]"]
                for task in tasks:
                    status = str(task.get("status", "pending"))
                    subject = escape(str(task.get("subject", "")))
                    task_id = task.get("id", "")
                    blocked = task.get("blocked_by", [])
                    blocked_text = (
                        f"  [dim]blocked by {escape(str(blocked))}[/dim]"
                        if blocked
                        else ""
                    )
                    lines.append(
                        f"{markers.get(status, '[?]')} #{task_id} {subject}{blocked_text}"
                    )
                body = "\n".join(lines)
            self._append(Static(body, classes="log-line"))
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]tasks error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示全部持久 Worker/Fleet 状态、attempt、预算和简短结果
    async def _show_workers(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "worker.list",
                {"limit": 50},
            )
            workers = list(result.get("workers", []))
            if not workers:
                body = "[dim]当前没有持久 Worker。[/dim]"
            else:
                markers = {
                    "queued": "[ ]",
                    "running": "[>]",
                    "waiting": "[~]",
                    "completed": "[x]",
                    "failed": "[!]",
                    "cancelled": "[-]",
                    "interrupted": "[|]",
                    "budget_limited": "[$]",
                }
                lines = ["[bold cyan]Workers[/bold cyan]"]
                for worker in workers:
                    status = str(worker.get("status", "queued"))
                    worker_id = escape(str(worker.get("worker_id", "")))
                    description = escape(str(worker.get("description", "")))
                    attempt = worker.get("attempt", 1)
                    maximum = worker.get("max_attempts", 1)
                    usage = worker.get("token_usage", 0)
                    budget = worker.get("token_budget")
                    budget_text = f"{usage}/{budget}" if budget else str(usage)
                    lines.append(
                        f"{markers.get(status, '[?]')} {worker_id} {description}  "
                        f"[dim]{status} · attempt {attempt}/{maximum} · tokens {budget_text}[/dim]"
                    )
                    summary = str(worker.get("summary", "")).strip()
                    if summary:
                        lines.append(f"    [dim]{escape(_preview(summary, 140))}[/dim]")
                body = "\n".join(lines)
            self._append(Static(body, classes="log-line"))
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]workers error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 从 Core durable ledger 加载 workflow 列表或单个 Work Graph projection
    async def _show_workflow(self, workflow_id: str = "") -> None:
        if self._client is None:
            return
        try:
            if workflow_id:
                result = await self._client.send_command(
                    "workflow.get",
                    {"workflow_id": workflow_id},
                )
                body = render_workflow_graph(dict(result.get("workflow", {})))
            else:
                result = await self._client.send_command(
                    "workflow.list",
                    {"limit": 50},
                )
                body = render_workflow_list(list(result.get("workflows", [])))
            self._append(Static(body, classes="log-line"))
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]workflow error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 读取本地 TOML/JSON IR 文件并通过 typed IPC 启动 durable workflow
    async def _start_workflow_file(self, raw_path: str) -> None:
        if self._client is None:
            return
        try:
            clean_path = raw_path.strip().strip('"').strip("'")
            path = Path(clean_path).expanduser()
            format_name = path.suffix.lower().removeprefix(".")
            if format_name not in {"json", "toml"}:
                raise ValueError("workflow file must use .json or .toml")
            source = path.read_text(encoding="utf-8")
            result = await self._client.send_command(
                "workflow.start",
                {"source": source, "format": format_name},
            )
            workflow_id = escape(str(result.get("workflow_id", "")))
            self._append(
                Static(
                    f"[green]Workflow 已启动[/green] {workflow_id}",
                    classes="log-line",
                )
            )
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(
                Static(f"[red]workflow start error: {exc}[/red]", classes="log-line")
            )
        finally:
            self._restore_ready_prompt()

    # 加载并展示当前工作区文件统计和统一 diff
    async def _show_diff(self) -> None:
        if self._client is None:
            return
        try:
            result = await self._client.send_command(
                "workspace.diff",
                {"scope": "all", "path": "."},
            )
            payload = dict(result.get("payload", {}))
            if "error" in payload:
                error = dict(payload["error"])
                self._append(
                    Static(
                        f"[red]diff error: {escape(str(error.get('message', 'unknown')))}[/red]",
                        classes="log-line",
                    )
                )
                return
            files = list(payload.get("files", []))
            additions = int(payload.get("additions", 0))
            deletions = int(payload.get("deletions", 0))
            self._append(
                Static(
                    f"[bold cyan]Diff[/bold cyan]  {len(files)} files  "
                    f"[green]+{additions}[/green] [red]-{deletions}[/red]",
                    classes="log-line",
                )
            )
            for file_info in files:
                path = escape(str(file_info.get("path", "")))
                index_status = escape(str(file_info.get("index_status", " ")))
                worktree_status = escape(str(file_info.get("worktree_status", " ")))
                self._append(
                    Static(
                        f"[dim]{index_status}{worktree_status}[/dim] {path}",
                        classes="log-line",
                    )
                )
            diff = str(payload.get("diff", ""))
            if diff:
                safe_diff = diff.replace("```", "` ` `")
                self._append(Markdown(f"```diff\n{safe_diff}\n```"))
            elif not files:
                self._append(Static("[dim]工作区没有改动。[/dim]", classes="log-line"))
        except (IpcError, RuntimeError, OSError, ValueError, TypeError) as exc:
            self._append(Static(f"[red]diff error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载最近一次 run 的可恢复 checkpoint 并打开选择器
    async def _show_rewind_picker(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "session.checkpoints",
                {"session_id": self._session_id},
            )
            checkpoints = [
                dict(item)
                for item in result.get("checkpoints", [])
                if item.get("status") == "ready"
            ]
            if not checkpoints:
                self._append(
                    Static("[dim]当前会话没有可恢复的 checkpoint。[/dim]", classes="log-line")
                )
                self._restore_ready_prompt()
                return
            self.mount(CheckpointPicker(checkpoints), before="#prompt")
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]rewind error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    # 关闭 checkpoint 选择器且不修改文件
    async def on_checkpoint_picker_dismissed(
        self,
        message: CheckpointPicker.Dismissed,
    ) -> None:
        message.picker.remove()
        self._restore_ready_prompt()

    # 恢复用户明确选中的 checkpoint 并显示实际恢复文件
    async def on_checkpoint_picker_selected(
        self,
        message: CheckpointPicker.Selected,
    ) -> None:
        message.picker.remove()
        if self._client is None or self._session_id is None:
            self._restore_ready_prompt()
            return
        try:
            result = await self._client.send_command(
                "session.rewind",
                {
                    "session_id": self._session_id,
                    "checkpoint_id": message.checkpoint_id,
                },
            )
            restored = [str(path) for path in result.get("restored", [])]
            unchanged = [str(path) for path in result.get("already_restored", [])]
            self._append(
                Static(
                    f"[bold yellow]Rewound[/bold yellow]  "
                    f"{escape(message.checkpoint_id)}  "
                    f"[dim]restored={escape(str(restored))} "
                    f"already={escape(str(unchanged))}[/dim]",
                    classes="log-line",
                )
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]rewind error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示当前会话上下文估算和最近一次真实模型占用率
    async def _show_context(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "session.context",
                {"session_id": self._session_id},
            )
            last_run = result.get("last_run_id") or "-"
            usage = dict(result.get("usage", {}))
            working_set = [str(path) for path in result.get("working_set", [])]
            compaction = result.get("compaction")
            compaction_data = dict(compaction) if isinstance(compaction, dict) else {}
            self._append(
                Static(
                    "[bold cyan]Context[/bold cyan]  "
                    f"messages={int(result.get('message_count', 0))}  "
                    f"estimated_tokens≈{int(result.get('estimated_tokens', 0))}  "
                    f"runs={int(result.get('run_count', 0))}\n"
                    f"[dim]last_run={escape(str(last_run))}[/dim]  "
                    f"{self._render_ctx_bar(self._last_context_pct)}\n"
                    f"usage={escape(json.dumps(usage, ensure_ascii=False))}  "
                    f"tool_schema≈{result.get('tool_schema_tokens') or 'unavailable'}  "
                    f"system≈{result.get('system_tokens') or 'unavailable'}  "
                    f"memories={int(result.get('memory_count', 0))}\n"
                    f"working_set={escape(', '.join(working_set) or 'empty')}\n"
                    "[dim]compaction="
                    f"{escape(json.dumps(compaction_data, ensure_ascii=False))}[/dim]",
                    classes="log-line",
                )
            )
        except (IpcError, RuntimeError, OSError, ValueError, TypeError) as exc:
            self._append(Static(f"[red]context error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载指定或当前最近 turn 的 durable inspector 投影
    async def _show_turn(self, turn_id: str = "") -> None:
        if self._client is None:
            return
        try:
            resolved_id = turn_id or self._active_run_id or ""
            if not resolved_id:
                if self._session_id is None:
                    raise ValueError("no current session")
                context = await self._client.send_command(
                    "session.context",
                    {"session_id": self._session_id},
                )
                resolved_id = str(context.get("last_run_id") or "")
            if not resolved_id:
                raise ValueError("current session has no turn")
            result = await self._client.send_command(
                "turn.inspect",
                {"turn_id": resolved_id},
            )
            self._append(
                Static(render_turn_inspector(dict(result)), classes="log-line")
            )
        except (IpcError, RuntimeError, OSError, ValueError, TypeError) as exc:
            self._append(Static(f"[red]turn error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    async def _show_session_picker(self) -> None:
        if self._client is None:
            return
        try:
            result = await self._client.send_command(
                "session.list",
                {"include_closed": True, "limit": 50},
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

    # 在 transcript 中展示用户级 route 列表和活动项，不显示 endpoint 路径或凭据正文
    def _show_provider_routes(self) -> None:
        routes = self._route_store.list()
        active = self._route_store.active()
        if not routes:
            self._append(
                Static(
                    "[yellow]尚未配置 Provider route，请使用 /config 添加。[/yellow]",
                    classes="log-line",
                )
            )
            return
        lines = ["[bold cyan]Provider routes[/bold cyan]"]
        for route in routes:
            marker = "[green]●[/green]" if active is not None and route.id == active.id else "○"
            meta = f"{route.wire_format} · {route.model}"
            if route.thinking != "off":
                meta += f" · thinking={route.thinking}"
            lines.append(
                f"{marker} [bold]{escape(route.id)}[/bold]  "
                f"[dim]{escape(meta)}[/dim]"
            )
        lines.append("[dim]切换：/provider <route-id>；thinking 档位在 routes.json 中配置[/dim]")
        self._append(Static("\n".join(lines), classes="log-line"))

    # 切换后续 turn 使用的活动 route，并立即刷新 TUI 顶栏
    def _select_provider_route(self, route_id: str) -> None:
        if not route_id:
            self._show_provider_routes()
            return
        try:
            route = self._route_store.set_active(route_id)
        except RouteStoreError as exc:
            self._append(Static(f"[red]{escape(str(exc))}[/red]", classes="log-line"))
            return
        self._route = route.id
        self._provider = route.id
        self._model = route.model
        self._models = [route.model]
        self._update_header("ready")
        self._append(
            Static(
                f"[green]活动 route[/green]  {escape(route.id)}/{escape(route.model)}",
                classes="log-line",
            )
        )

    # 更新活动 route 的模型字段，保留 provider、wire、endpoint 和凭据引用
    def _select_route_model(self, model: str) -> None:
        selected = model.strip()
        if not selected:
            return
        active = self._route_store.active()
        if active is None:
            self._append(
                Static(
                    "[yellow]尚无活动 route，请先使用 /config 配置 Provider。[/yellow]",
                    classes="log-line",
                )
            )
            return
        payload = active.model_dump(mode="python")
        payload["model"] = selected
        updated = ProviderRoute.model_validate(payload)
        self._route_store.update(updated)
        self._route = updated.id
        self._model = updated.model
        if selected not in self._models:
            self._models.append(selected)
        self._update_header("ready")
        self._append(
            Static(
                f"[green]活动模型[/green]  {escape(updated.id)}/{escape(updated.model)}",
                classes="log-line",
            )
        )

    # 对活动 route 执行最小真实探测，并展示脱敏分类结果
    async def _show_provider_doctor(self) -> None:
        try:
            route = self._route_store.active()
            if route is None:
                self._append(
                    Static(
                        "[yellow]尚无活动 route，请先使用 /config 配置 Provider。[/yellow]",
                        classes="log-line",
                    )
                )
                return
            credential = self._credential_store.resolve(route.credential_ref)
            result = await self._provider_doctor.check(route, credential)
            color = "green" if result.status == "ok" else "red"
            status = escape(result.status)
            category = escape(result.category)
            message = escape(result.message)
            self._append(
                Static(
                    f"[bold cyan]Provider doctor[/bold cyan]  [{color}]{status}[/{color}]  "
                    f"{category}\n[dim]{message} · credential={result.credential_source}[/dim]",
                    classes="log-line",
                )
            )
        finally:
            self._restore_ready_prompt()

    # 将已探测的内置 Provider 选择保存为 route，并在当前 TUI 内立即生效
    def _save_config_route(self, provider: ProviderPreset, api_key: str, model: str) -> None:
        provider_kind = (
            "anthropic"
            if provider.id == "anthropic"
            else "openai"
            if provider.id == "openai"
            else "openai-compatible"
        )
        wire_format = (
            "anthropic_messages" if provider.anthropic_api else "openai_chat"
        )
        credential_ref = self._credential_store.save(provider.id, api_key)
        route = ProviderRoute.model_validate(
            {
                "id": provider.id,
                "provider": provider_kind,
                "wire_format": wire_format,
                "base_url": provider.chat_url or "https://api.anthropic.com",
                "model": model,
                "credential_ref": credential_ref,
                "supports_prompt_cache": provider.anthropic_api,
            }
        )
        if any(item.id == route.id for item in self._route_store.list()):
            self._route_store.update(route)
            self._route_store.set_active(route.id)
        else:
            self._route_store.add(route, activate=True)
        self._provider = provider.id
        self._route = route.id
        self._model = route.model
        self._models = list(self._discovered_config_models)
        self._pending_config_key = None
        self._discovered_config_models = ()
        self._config_provider = None
        self._update_header("ready")
        self._append(
            Static(
                f"[green]Provider 已配置[/green]  {escape(route.id)}/{escape(route.model)}",
                classes="log-line",
            )
        )
        self._restore_ready_prompt()

    # 关闭模型选择器并恢复聊天输入框
    async def on_model_picker_dismissed(self, message: ModelPicker.Dismissed) -> None:
        message.picker.remove()
        self._config_provider = None
        self._pending_config_key = None
        self._discovered_config_models = ()
        self._restore_ready_prompt()

    # 选择模型后直接更新用户级 route，当前页面和下一 turn 立即生效
    async def on_model_picker_selected(self, message: ModelPicker.Selected) -> None:
        message.picker.remove()
        if self._config_provider is not None and self._pending_config_key is not None:
            self._save_config_route(
                self._config_provider,
                self._pending_config_key,
                message.model,
            )
            return
        if message.model == self._model:
            self._restore_ready_prompt()
            return
        self._select_route_model(message.model)
        self._restore_ready_prompt()

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

    # 重命名当前会话并同步本地标题状态；自动标题模式不输出提示行
    async def _do_rename_session(self, title: str, *, announce: bool = True) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "session.rename",
                {"session_id": self._session_id, "title": title},
            )
            session = result.get("session", {})
            self._session_title = str(session.get("title", "") or title)
            self._titled = True
            if announce:
                self._append(
                    Static(
                        f"[green]会话已重命名[/green]  {escape(self._session_title)}",
                        classes="log-line",
                    )
                )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]rename error: {exc}[/red]", classes="log-line"))
        finally:
            if announce:
                self._restore_ready_prompt()

    # 复制当前会话为分支并切换到新会话
    async def _do_fork_session(self, title: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "session.fork",
                {"session_id": self._session_id, "title": title},
            )
            session = result.get("session", {})
            forked_id = str(session.get("session_id", ""))
            if not forked_id:
                raise ValueError("fork 结果缺少 session_id")
            await self._switch_session(forked_id)
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._append(Static(f"[red]fork error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    # 导出当前会话到工作区文件
    async def _do_export_session(self, fmt: str) -> None:
        if self._client is None or self._session_id is None:
            return
        format_name = "json" if fmt == "json" else "markdown"
        try:
            result = await self._client.send_command(
                "session.export",
                {"session_id": self._session_id, "format": format_name},
            )
            filename = str(result.get("filename", ""))
            content = str(result.get("content", ""))
            suffix = "json" if format_name == "json" else "md"
            target = Path.cwd() / (
                filename or f"coderook-session-{self._session_id}.{suffix}"
            )
            target.write_text(content, encoding="utf-8")
            self._append(
                Static(
                    f"[green]会话已导出[/green]  {escape(str(target))}",
                    classes="log-line",
                )
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]export error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 删除当前会话并自动新建空会话
    async def _do_delete_session(self) -> None:
        if self._client is None or self._session_id is None:
            return
        session_id = self._session_id
        try:
            await self._client.send_command("session.delete", {"session_id": session_id})
            self._append(
                Static(
                    f"[green]会话已删除[/green]  {escape(session_id)}",
                    classes="log-line",
                )
            )
            await self._create_and_switch_session()
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]delete error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    # 首个 run 成功后按首条用户消息自动生成会话标题
    def _maybe_autotitle_session(self) -> None:
        if (
            self._titled
            or not self._first_user_text
            or self._client is None
            or self._session_id is None
        ):
            return
        title = _derive_session_title(self._first_user_text)
        if not title:
            return
        self._titled = True
        self._session_title = title
        self.run_worker(
            self._do_rename_session(title, announce=False),
            name="autotitle_session",
            exclusive=False,
        )

    async def _switch_session(self, session_id: str) -> None:
        if self._client is None:
            return
        if session_id == self._session_id:
            self._restore_ready_prompt()
            return
        try:
            resumed = await self._client.send_command("session.resume", {"session_id": session_id})
            info = resumed.get("session", {})
            title = str(info.get("title", "")) if isinstance(info, dict) else ""
            await self._load_session(session_id, resume=True, title=title)
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]session switch error: {exc}[/red]", classes="log-line"))
            self._restore_ready_prompt()

    async def _load_session(
        self,
        session_id: str,
        *,
        resume: bool,
        title: str | None = None,
    ) -> None:
        if self._client is None:
            return
        self._clear_plan_review()
        self._clear_user_question()
        history = await self._client.send_command(
            "session.get_history",
            {"session_id": session_id},
        )
        log_view = self.query_one("#log-view", VerticalScroll)
        await log_view.remove_children()
        self._session_id = session_id
        self._resume_session_id = session_id
        self._history_loaded = True
        self._session_title = title or ""
        self._titled = bool(title and title != "Untitled")
        self._first_user_text = ""
        self._reset_cost_state()
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
                prompt.border_title = _PROMPT_READY
            self._update_header("ready")
            self._append(Static(f"[red]send error: {e}[/red]", classes="log-line"))

    # 将用户运行中纠偏发送给当前活动 run
    async def _do_steer(self, run_id: str, content: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "run.steer",
                {"run_id": run_id, "content": content},
            )
            prompt = self._prompt()
            if prompt is not None and self._busy:
                prompt.border_title = "补充要求已发送"
                prompt.focus()
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]steering error: {exc}[/red]", classes="log-line"))

    # 将选项或自由文本答案发送给挂起的结构化问题
    async def _do_answer_question(self, question_id: str, answer: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command(
                "user_question.respond",
                {"question_id": question_id, "answer": answer},
            )
            self._pending_question_id = None
            self._answering_question = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.border_title = "回答已发送 · Agent 继续执行"
                prompt.focus()
        except (IpcError, RuntimeError, OSError) as exc:
            self._answering_question = True
            self._append(Static(f"[red]question answer error: {exc}[/red]", classes="log-line"))
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.border_title = _PROMPT_QUESTION
                prompt.focus()

    # 处理结构化问题选项；自由回答分支把焦点交给主输入框
    async def on_user_question_select_answered(
        self,
        message: UserQuestionSelect.Answered,
    ) -> None:
        message.select.remove()
        prompt = self._prompt()
        if message.answer is None:
            self._answering_question = True
            if prompt is not None:
                prompt.disabled = False
                prompt.border_title = _PROMPT_QUESTION
                prompt.focus()
            return
        self._answering_question = False
        if prompt is not None:
            prompt.disabled = True
            prompt.border_title = "正在发送回答"
        self._append(
            Static(
                f"[bold cyan]answer >[/bold cyan] {escape(message.answer)}",
                classes="user-turn",
            )
        )
        self.run_worker(
            self._do_answer_question(message.select.question_id, message.answer),
            name="answer_question",
            exclusive=False,
        )

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
            await self._select_runtime_mode(RuntimeMode.ACT, announce=False)
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
            await self._select_runtime_mode(RuntimeMode.PLAN, announce=False)
            prompt = self._prompt()
            if prompt is not None:
                prompt.text = "/plan "
                prompt.move_cursor(prompt.document.end)
                prompt.border_title = "规划模式 · 输入反馈"
            self._update_header("plan")
        else:
            await self._select_runtime_mode(RuntimeMode.ACT, announce=False)
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
                perm_block.remove()
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
                    p.border_title = _PROMPT_READY
                    p.focus()
        except Exception:
            log.exception("on_permission_select_decided failed tool_use_id=%s", tool_use_id)

    # 向日志视图追加一个 widget；用户上滚离开底部时暂停自动跟随
    def _append(self, widget: Widget) -> None:
        log_view = self.query_one("#log-view", VerticalScroll)
        follow = log_view.is_vertical_scroll_end
        log_view.mount(widget)
        if follow:
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
                        Static(escape(content), classes="user-turn")
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
                    params_raw = block.get("input", {})
                    params = params_raw if isinstance(params_raw, dict) else {}
                    action = escape(
                        _tool_action_text(
                            str(block.get("name", "")),
                            params,
                            finished=True,
                        )
                    )
                    self._append(
                        Static(
                            f"[green]✓[/green] [#aab2be]{action}[/#aab2be]",
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

    # 清除当前结构化问题及自由回答状态
    def _clear_user_question(self) -> None:
        self._pending_question_id = None
        self._answering_question = False
        try:
            self.query_one(UserQuestionSelect).remove()
        except Exception:
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

    # 生成 context 占用率的彩色进度条字符串，宽度可配以适配顶栏
    def _render_ctx_bar(self, pct: float, width: int = 20) -> str:
        width = max(4, width)
        filled = int(pct * width)
        bar = "█" * filled + "░" * (width - filled)
        label = f"ctx:{pct * 100:.0f}%"
        if pct >= 0.85:
            color = "bold red"
        elif pct >= 0.70:
            color = "yellow"
        else:
            color = "dim"
        return f"[{color}]{label} {bar}[/{color}]"

    # 复位会话级成本累计状态
    def _reset_cost_state(self) -> None:
        self._cost_total = 0.0
        self._cost_by_model = {}
        self._tokens_by_model = {}
        self._cache_saved_total = 0.0
        self._unpriced_models = set()

    # 把一次 llm.usage 的用量折算为成本并累计到会话分解中
    def _accumulate_cost(self, event: dict[str, Any]) -> None:
        model = str(event.get("model", "") or self._model or "unknown")
        pricing = get_pricing(model, self._pricing_overrides)
        if pricing is None:
            self._unpriced_models.add(model)
            return
        input_tokens = int(event.get("input_tokens", 0) or 0)
        output_tokens = int(event.get("output_tokens", 0) or 0)
        cache_read = int(event.get("cache_read_input_tokens", 0) or 0)
        cache_write = int(event.get("cache_creation_input_tokens", 0) or 0)
        cost = estimate_cost(
            pricing,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        self._cost_total += cost
        self._cost_by_model[model] = self._cost_by_model.get(model, 0.0) + cost
        bucket = self._tokens_by_model.setdefault(
            model,
            {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        )
        bucket["input"] += input_tokens
        bucket["output"] += output_tokens
        bucket["cache_read"] += cache_read
        bucket["cache_write"] += cache_write
        self._cache_saved_total += cache_read_savings(pricing, cache_read)

    # 在日志中渲染本会话成本分解：总额、按模型、缓存节省与无价模型提示
    def _show_cost_breakdown(self) -> None:
        lines = ["[bold cyan]Cost[/bold cyan]  本 TUI 进程内累计"]
        lines.append(f"  总计 [bold]{format_cost(self._cost_total)}[/bold]")
        if self._cost_by_model:
            for model in sorted(self._cost_by_model):
                bucket = self._tokens_by_model[model]
                cost = self._cost_by_model[model]
                lines.append(
                    f"  {escape(model)}  [bold]{format_cost(cost)}[/bold]"
                    f"  [dim]in={bucket['input']} out={bucket['output']} "
                    f"cache_read={bucket['cache_read']} "
                    f"cache_write={bucket['cache_write']}[/dim]"
                )
        if self._cache_saved_total > 0:
            lines.append(
                f"  缓存命中节省约 [green]{format_cost(self._cache_saved_total)}[/green]"
            )
        if self._unpriced_models:
            names = ", ".join(sorted(self._unpriced_models))
            lines.append(
                f"  [yellow]{escape(names)} 无单价，未计入；"
                "可在 ~/.coderook/pricing.toml 配置[/yellow]"
            )
        if not self._cost_by_model and not self._unpriced_models:
            lines.append("  [dim]本会话还没有可计费的模型用量。[/dim]")
        lines.append(
            "[dim]单价为内置参考价，可在 ~/.coderook/pricing.toml 用 "
            "[models.\"<id>\"] input=3.0 output=15.0 覆盖；"
            "子代理用量未计入本视图。[/dim]"
        )
        self._append(Static("\n".join(lines), classes="log-line"))

    # 根据连接和运行状态刷新顶部标题，并常驻显示 context 水位
    def _update_header(self, state: str) -> None:
        self._header_state = state
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        session = f"  [dim]{self._session_id}[/dim]" if self._session_id else ""
        route_label = "/".join(part for part in (self._route, self._model) if part)
        model = f"  [cyan]{escape(route_label)}[/cyan]" if route_label else ""
        permission_labels = {
            "ask": "ask",
            "accept_edits": "accept edits",
            "full_access": "full access",
        }
        mode = f"  [blue]{self._input_runtime_mode.value}[/blue]"
        permission = (
            f"  [magenta]{permission_labels[self._authority_preset]}[/magenta]"
        )
        trust_color = "green" if self._workspace_trust == WorkspaceTrust.TRUSTED else "yellow"
        trust = f"  [{trust_color}]{self._workspace_trust.value}[/{trust_color}]"
        ctx = f"  {self._render_ctx_bar(self._last_context_pct, width=10)}"
        cost = (
            f"  [dim]${self._cost_total:.4f}[/dim]"
            if self._cost_total >= 0.0001
            else ""
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
            f"{session}{model}{mode}{permission}{trust}{ctx}{cost}"
            f"  [{color}]{state}[/{color}]"
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
                        "user_question.*",
                        "lsp.*",
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
                    self._session_title = ""
                    self._titled = False
                    self._first_user_text = ""
                    log.info("session created session_id=%s", self._session_id)
                else:
                    resumed = await client.send_command(
                        "session.resume", {"session_id": self._resume_session_id}
                    )
                    resumed_info = resumed.get("session", {})
                    resumed_title = (
                        str(resumed_info.get("title", ""))
                        if isinstance(resumed_info, dict)
                        else ""
                    )
                    self._session_id = str(resumed["session"]["session_id"])
                    self._session_title = resumed_title
                    self._titled = bool(resumed_title and resumed_title != "Untitled")
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
                        prompt.border_title = "审阅上方计划"
                    elif self._input_runtime_mode == RuntimeMode.PLAN:
                        prompt.border_title = _PROMPT_PLAN
                    else:
                        prompt.border_title = _PROMPT_READY
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
                    prompt.border_title = "连接已断开 · 正在重试"
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

        if t == "llm.reasoning":
            content = str(event.get("content") or "")
            self._break_llm()
            if content.strip():
                reasoning_block = LLMStreamBlock()
                reasoning_block.append_token(content)
                reasoning_block.set_kind("reasoning")
                reasoning_block.finalize_markdown()
                self._append(reasoning_block)
            return

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
            if self._current_llm is not None:
                self._current_llm.set_kind("answer" if intent == "respond" else intent)
            self._break_llm()
            return

        if t == "llm.usage":
            run_id = event.get("run_id", "")
            if run_id in self._subagent_run_ids:
                return
            pct = float(event.get("context_pct") or 0.0)
            self._last_context_pct = pct
            self._accumulate_cost(event)
            self._update_header(self._header_state)
            return

        self._break_llm()

        if t == "llm.route_selected":
            self._route = str(event.get("route_id") or "")
            self._model = str(event.get("model") or "")
            self._update_header("running")

        elif t == "llm.retry":
            kind = str(event.get("kind") or "retry")
            attempt = int(event.get("attempt") or 0)
            self._append(
                Static(
                    f"[dim]正在重试模型响应  {escape(kind)} #{attempt}[/dim]",
                    classes="log-line",
                )
            )

        elif t == "agent.stuck":
            tool_name = escape(str(event.get("tool_name") or "tool"))
            repeat_count = int(event.get("repeat_count") or 0)
            self._append(
                Static(
                    f"[yellow]stopped repeated action[/yellow]  "
                    f"[bold]{tool_name}[/bold]  [dim]{repeat_count} identical results[/dim]",
                    classes="log-line",
                )
            )

        elif t == "session.waiting_for_input":
            self._busy = False
            self._cancel_requested = False
            self._cancel_armed = False
            self._clear_user_question()
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = self._plan_review_pending
                prompt.read_only = False
                if self._plan_review_pending:
                    prompt.border_title = "审阅上方计划"
                else:
                    prompt.border_title = _PROMPT_READY
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
                prompt.border_title = "审阅上方计划"
            self.mount(PlanReview(run_id), before="#prompt")
            self._update_header("plan ready")

        elif t == "plan.updated":
            raw_plan = event.get("plan", [])
            plan = raw_plan if isinstance(raw_plan, list) else []
            markers = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }
            lines = ["[bold cyan]Plan updated[/bold cyan]"]
            explanation = str(event.get("explanation", "")).strip()
            if explanation:
                lines.append(f"[dim]{escape(explanation)}[/dim]")
            for item in plan:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "pending"))
                marker = markers.get(status, "[ ]")
                lines.append(f"{marker} {escape(str(item.get('step', '')))}")
            self._append(Static("\n".join(lines), classes="log-line"))

        elif t == "user_question.asked":
            session_id = str(event.get("session_id", ""))
            if session_id != self._session_id:
                return
            question_id = str(event.get("question_id", ""))
            self._pending_question_id = question_id
            self._answering_question = False
            try:
                self.query_one(UserQuestionSelect).remove()
            except NoMatches:
                pass
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.border_title = _PROMPT_QUESTION
            self.mount(
                UserQuestionSelect(
                    question_id,
                    str(event.get("question", "")),
                    str(event.get("header", "Question")),
                    [str(option) for option in event.get("options", [])],
                    bool(event.get("multi_select", False)),
                ),
                before="#prompt",
            )
            self._update_header("question")

        elif t == "session.interrupted":
            self._busy = False
            self._active_run_id = None
            self._cancel_requested = False
            self._cancel_armed = False
            self._clear_user_question()
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.read_only = False
                prompt.border_title = "任务已取消"
                prompt.focus()
            self._update_header("interrupted")

        elif t == "session.closed":
            self._busy = False
            self._cancel_requested = False
            self._clear_user_question()
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = True
                prompt.read_only = False
                prompt.border_title = "会话已关闭"
            self._update_header("disconnected")

        elif t == "run.started":
            run_id = str(event.get("run_id", ""))
            self._active_run_id = run_id
            self._current_steps.pop(run_id, None)
            self._cancel_requested = False
            self._cancel_armed = False

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
            run_id = str(event.get("run_id", ""))
            if run_id in self._subagent_run_ids:
                return
            self._current_steps[run_id] = int(event.get("step") or 0)

        elif t == "step.finished":
            run_id = str(event.get("run_id", ""))
            step = int(event.get("step") or self._current_steps.get(run_id, 0))
            self._tool_step_groups.pop((run_id, step), None)
            if self._current_steps.get(run_id) == step:
                self._current_steps.pop(run_id, None)

        elif t == "tool.call_started":
            tool_use_id = str(event.get("tool_use_id", ""))
            tool_name = str(event.get("tool_name", ""))
            raw_params = event.get("params") or {}
            params = raw_params if isinstance(raw_params, dict) else {}
            run_id = str(event.get("run_id", ""))
            tc_block = ToolCallBlock(tool_name, params)
            if run_id in self._subagent_run_ids:
                tc_block.styles.padding = (0, 2, 0, 6)
                self._append(tc_block)
            else:
                step = self._current_steps.get(run_id, 0)
                group_key = (run_id, step)
                group = self._tool_step_groups.get(group_key)
                if group is None:
                    group = ToolStepGroup(step)
                    self._tool_step_groups[group_key] = group
                    group.add_tool(tc_block)
                    self._append(group)
                else:
                    group.add_tool(tc_block)
            self._pending_tool_blocks[tool_use_id] = tc_block

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
            if event.get("terminal") is False:
                return
            if tool_use_id in self._pending_tool_blocks:
                tc_done = self._pending_tool_blocks.pop(tool_use_id)
                tc_done.set_result(error_msg, elapsed_ms, is_error=True)

        elif t == "run.finished":
            status = event.get("status", "")
            steps = event.get("steps", 0)
            step_label = "step" if steps == 1 else "steps"
            reason = event.get("reason") or ""
            run_id = str(event.get("run_id", ""))
            self._current_steps.pop(run_id, None)
            for group_key in [key for key in self._tool_step_groups if key[0] == run_id]:
                self._tool_step_groups.pop(group_key, None)
            self._active_run_id = None
            self._cancel_requested = False
            self._cancel_armed = False
            if status == "success":
                self._maybe_autotitle_session()
                return
            if reason == "cancelled":
                self._append(Static(
                    f"[yellow]–[/yellow] [dim]Cancelled after {steps} {step_label}[/dim]",
                    classes="run-err",
                ))
            else:
                detail = f"  [dim]{reason}[/dim]" if reason else ""
                self._append(Static(
                    f"[red]×[/red] [dim]Failed after {steps} {step_label}[/dim]{detail}",
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
                prompt.border_title = _PROMPT_PERMISSION
            self._append(perm_block)
            select = PermissionSelect(tool_use_id, tool_name, param_preview, params)
            self._mount_permission_select(select)
            log.debug(
                "PermissionSelect mounted before #prompt  pending=%d",
                len(self._pending_permission_blocks),
            )

        elif t == "permission.denied":
            # 处理超时或断连等非用户交互触发的 deny，失败结果由工具行统一展示。
            tool_use_id = str(event.get("tool_use_id", ""))
            if tool_use_id in self._pending_permission_blocks:
                perm_block = self._pending_permission_blocks.pop(tool_use_id)
                denied_tool = escape(perm_block._tool_name)
                self._append(
                    Static(
                        f"[yellow]审批超时或连接断开，{denied_tool} 已按拒绝处理[/yellow]",
                        classes="log-line",
                    )
                )
                perm_block.remove()
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
                        p.border_title = _PROMPT_READY
                        p.focus()

        elif t == "lsp.diagnostics":
            status = str(event.get("status", ""))
            tool = escape(str(event.get("tool", "")))
            count = int(event.get("diagnostic_count", 0))
            paths = ", ".join(str(p) for p in event.get("paths", [])[:3])
            if status == "ok" and count == 0:
                line = f"[green]诊断通过[/green]  [dim]{tool} · {escape(paths)}[/dim]"
            elif status == "ok":
                line = (
                    f"[yellow]诊断发现 {count} 条问题[/yellow]  "
                    f"[dim]{tool} · {escape(paths)}[/dim]"
                )
            else:
                error = escape(str(event.get("error", ""))[:120])
                line = f"[dim]诊断降级 {status} · {tool} · {error}[/dim]"
            self._append(Static(line, classes="log-line"))

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

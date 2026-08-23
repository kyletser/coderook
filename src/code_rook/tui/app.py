from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from rich.markup import escape
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Label, Markdown, Static

from code_rook.core.artifacts import ArtifactError, ArtifactStore, inspect_image
from code_rook.core.authority import AuthorityProfile, RuntimeMode, WorkspaceTrust
from code_rook.core.config import CodeRookConfig
from code_rook.core.configuration import (
    ConfigurationService,
    ConfigurationValidationError,
)
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
from code_rook.tui import ipc_actions
from code_rook.tui.clipboard import copy_to_windows_clipboard
from code_rook.tui.commands import (
    BUILTIN_SLASH_COMMANDS,
    complete_command_arg_text,
    match_slash_command,
)
from code_rook.tui.connection import TuiConnection
from code_rook.tui.ipc_actions import IpcActionError
from code_rook.tui.panels import (
    render_artifact_gc,
    render_artifacts,
    render_hooks,
    render_job_output,
    render_jobs,
    render_mcp_servers,
    render_mcp_tools,
    render_memory,
    render_turn_inspector,
    render_workers_summary,
    render_workflow_graph,
    render_workflow_list,
)
from code_rook.tui.render import (
    _PROMPT_QUESTION,
    _PROMPT_READY,
    render_event,
)
from code_rook.tui.widgets import (
    _param_summary as _param_summary,
)
from code_rook.tui.widgets import (
    _preview as _preview,
)
from code_rook.tui.widgets import (
    _tool_action_text as _tool_action_text,
)
from code_rook.tui.widgets import (
    _tool_target as _tool_target,
)
from code_rook.tui.widgets.actions import ConfigSwitch as ConfigSwitch
from code_rook.tui.widgets.actions import ModelSwitch as ModelSwitch
from code_rook.tui.widgets.input import (
    ChatTextArea,
    CompletionItem,
    ConfigApiKeyPrompt,
    SlashCompleteWidget,
    _load_input_history,
)
from code_rook.tui.widgets.input import (
    _save_input_history_entry as _save_input_history_entry,
)
from code_rook.tui.widgets.permission import (
    _MODE_CYCLE,
    _PERMISSION_PRESETS,
    PermissionBlock,
    PermissionModePicker,
    PermissionSelect,
)
from code_rook.tui.widgets.pickers import CheckpointPicker, PlanReview, UserQuestionSelect
from code_rook.tui.widgets.selectors import ModelPicker, ProviderPicker, SessionPicker
from code_rook.tui.widgets.stream import LLMStreamBlock, ToolCallBlock, ToolStepGroup

log = logging.getLogger(__name__)

_PROMPT_CONNECTING = "正在连接"
_PROMPT_PLAN = "规划模式"
_PROMPT_RUNNING = "执行中 · Enter 补充 · Ctrl+C 取消"
_PROMPT_PLANNING = "规划中 · Enter 补充 · Ctrl+C 取消"




# 从首条用户消息派生简洁会话标题；斜杠命令不作为标题来源
def _derive_session_title(text: str, max_len: int = 20) -> str:
    cleaned = " ".join(text.split())
    if not cleaned or cleaned.startswith("/"):
        return ""
    return _preview(cleaned, max_len)




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
        continue_recent: bool = False,
        auth_token: str | None = None,
        provider: str = "",
        model: str = "",
        models: list[str] | None = None,
        route: str = "",
        route_store: RouteStore | None = None,
        credential_store: CredentialStore | None = None,
        provider_doctor: ProviderDoctor | None = None,
        core_recovery: Callable[[], object] | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._workspace = Path.cwd().resolve()
        self._replay_run_id = replay_run_id
        self._resume_session_id = resume_session_id
        self._continue_recent = continue_recent
        self._auth_token = auth_token
        self._provider = provider
        self._route = route
        self._model = model
        self._models = models or ([model] if model else [])
        self._route_store = route_store or RouteStore()
        self._credential_store = credential_store or CredentialStore()
        self._configuration = ConfigurationService(
            self._route_store,
            self._credential_store,
        )
        self._provider_doctor = provider_doctor or ProviderDoctor()
        self._core_recovery = core_recovery
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
        self._active_goal_id: str | None = None
        self._active_goal_status: str = ""
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
        self._slash_items: list[CompletionItem] = []
        self._artifact_store = ArtifactStore(Path.cwd() / ".coderook" / "artifacts")
        self._pending_image_attachments: list[dict[str, object]] = []
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time
        self._product_notice_keys: set[str] = set()

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
        self._connection = TuiConnection(
            self,
            self._handle_event,
            host=self._host,
            port=self._port,
            auth_token=self._auth_token,
        )
        self.run_worker(self._connection.run(), exclusive=True, name="socket")

    # 连接建立并完成会话恢复后：还原输入框状态与顶栏
    def _mark_connected(self) -> None:
        self._clear_connection_problems()
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
        self._show_startup_state()

    # 连接断开后：禁用输入框并提示正在重试
    def _mark_disconnected(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = True
            prompt.read_only = False
            prompt.border_title = "连接已断开 · 正在重试"

    # 向 transcript 追加一次去重的产品状态说明，避免重连循环刷屏
    def _show_product_notice(self, key: str, message: str) -> None:
        if key in self._product_notice_keys:
            return
        self._product_notice_keys.add(key)
        self._append(Static(message, classes="log-line"))

    # 展示连接问题及用户可执行的恢复动作
    def _show_connection_problem(self, kind: str, detail: str) -> None:
        safe_detail = escape(detail)
        if kind == "authentication":
            message = (
                "[bold red]Core authentication failed[/bold red]  "
                f"[dim]{safe_detail}[/dim]\n"
                "[dim]确认 daemon 与 TUI 使用同一 ipc-token；修复后会自动重试。[/dim]"
            )
        elif kind == "recovering":
            message = (
                "[bold green]Core restarted[/bold green]  "
                f"[dim]{safe_detail}；正在恢复当前会话。[/dim]"
            )
        elif kind == "protocol":
            message = (
                "[bold red]Core request failed[/bold red]  "
                f"[dim]{safe_detail}[/dim]\n"
                "[dim]检查 session ID 与客户端版本；必要时运行 "
                "coderook core restart。[/dim]"
            )
        else:
            message = (
                "[bold yellow]Core unavailable[/bold yellow]  "
                f"[dim]{safe_detail}[/dim]\n"
                "[dim]输入暂不可用；运行 coderook core start 后本界面会自动重连。[/dim]"
            )
        self._show_product_notice(f"connection:{kind}", message)

    # 显示会话新建、首次恢复或断线续接结果，让用户明确当前上下文来源
    def _show_session_ready(
        self,
        action: str,
        session_id: str,
        title: str,
        history_count: int | None,
    ) -> None:
        labels = {
            "created": "New session",
            "resumed": "Session resumed",
            "reconnected": "Session reconnected",
        }
        label = labels.get(action, "Session ready")
        short_id = escape(session_id)
        title_text = f"  [bold]{escape(title)}[/bold]" if title else ""
        history_text = (
            f"  [dim]{history_count} history message(s)[/dim]"
            if history_count is not None
            else ""
        )
        message = f"[cyan]{label}[/cyan]  [dim]{short_id}[/dim]{title_text}{history_text}"
        if action == "reconnected":
            self._append(Static(message, classes="log-line"))
        else:
            self._show_product_notice(f"session:{action}:{session_id}", message)

    # 连接恢复后允许未来的新一轮故障再次显示一次说明
    def _clear_connection_problems(self) -> None:
        self._product_notice_keys = {
            key for key in self._product_notice_keys if not key.startswith("connection:")
        }

    # 首次连接后非阻塞地说明无模型与降级隔离状态
    def _show_startup_state(self) -> None:
        if not self._route and not self._model:
            self._show_product_notice(
                "startup:no-model",
                "[bold yellow]No active model[/bold yellow]  "
                "[dim]会话浏览、帮助和管理命令仍可使用；执行 Agent 任务前可用 "
                "/config 或 /provider 配置路由。[/dim]",
            )
        if not bool(self._sandbox.get("available", False)):
            kind = escape(str(self._sandbox.get("kind", "none")))
            reason = escape(str(self._sandbox.get("reason", "unavailable")))
            self._show_product_notice(
                "startup:degraded-sandbox",
                "[bold yellow]Sandbox DEGRADED[/bold yellow]  "
                f"[dim]{kind}: {reason}[/dim]\n"
                "[dim]当前没有 OS 强制隔离；危险动作继续走 ASK 审批和工作区边界，"
                "但这些机制不等同于系统沙箱。[/dim]",
            )

    # 构建斜杠命令候选列表：内建命令（含 usage）+ 所有已注册 skill（无 usage）
    def _build_slash_items(self) -> list[CompletionItem]:
        items: list[CompletionItem] = [
            CompletionItem(cmd.name, cmd.description, cmd.usage)
            for cmd in BUILTIN_SLASH_COMMANDS
        ]
        try:
            loader = SkillLoader()
            for skill in loader.list_all_skills():
                desc = skill.description.splitlines()[0] if skill.description else ""
                if len(desc) > 60:
                    desc = desc[:57] + "..."
                items.append(CompletionItem(skill.name, desc))
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
        prompt = self._prompt()
        if prompt is not None and self._try_complete_command_arg(prompt.text):
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

    # 输入形如 /命令 <待补参数> 时按 Tab 循环补全命令参数；返回是否发生了补全
    def _try_complete_command_arg(self, text: str) -> bool:
        replacement = complete_command_arg_text(text)
        if replacement is None:
            return False
        prompt = self._prompt()
        if prompt is None:
            return False
        prompt.text = replacement
        prompt.move_cursor(prompt.document.end)
        return True

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
        for item in self._slash_items:
            lines.append(
                f"  [cyan]/{item.name}[/cyan]  [dim]{escape(item.description)}[/dim]"
            )
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
        if not content and self._pending_image_attachments:
            content = "请分析附加图片。"
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
        cmd = match_slash_command(content)
        if cmd is not None and cmd.name == "goal":
            await cmd.handler(self, event.text_area, content)
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
        if cmd is not None:
            await cmd.handler(self, event.text_area, content)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(
                Static("[yellow]Agent 忙碌或未连接，请稍后再试[/yellow]", classes="log-line")
            )
            return
        self._begin_message(event.text_area, content, self._input_runtime_mode)

    # 收到输入框图片粘贴后异步校验并写入内容寻址 ArtifactStore
    async def on_chat_text_area_image_pasted(
        self,
        event: ChatTextArea.ImagePasted,
    ) -> None:
        if len(self._pending_image_attachments) >= 8:
            self.notify("每条消息最多附加 8 张图片", severity="warning")
            return
        self.run_worker(
            self._stage_pasted_image(event.path),
            name="stage_pasted_image",
            exclusive=False,
        )

    # 读取图片头、落 artifact 并显示尺寸、类型和短 hash
    async def _stage_pasted_image(self, path: Path) -> None:
        try:
            data = await asyncio.to_thread(path.read_bytes)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("图片超过 2 MiB，请先压缩或裁剪")
            metadata = inspect_image(data)
            reference = await self._artifact_store.put(
                data,
                media_type=metadata.media_type,
            )
        except (ArtifactError, OSError, ValueError) as exc:
            self.notify(f"图片附加失败：{exc}", severity="error")
            return
        attachment: dict[str, object] = {
            "sha256": reference.sha256,
            "media_type": metadata.media_type,
            "size": reference.size,
            "width": metadata.width,
            "height": metadata.height,
        }
        if attachment not in self._pending_image_attachments:
            self._pending_image_attachments.append(attachment)
        self._append(
            Static(
                "[cyan]已附加图片[/cyan] "
                f"{escape(path.name)} · {metadata.width}x{metadata.height} · "
                f"{metadata.media_type} · [dim]{reference.sha256[:12]}[/dim]",
                classes="log-line",
            )
        )

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
        attachments = list(self._pending_image_attachments)
        self._pending_image_attachments.clear()
        self.run_worker(
            self._do_send_message(content, runtime_mode, attachments=attachments),
            name="send_message",
            exclusive=False,
        )

    # 进入 Goal 创建或恢复运行态，并在 worker 中执行长生命周期 IPC
    def _begin_goal_command(
        self,
        prompt: ChatTextArea,
        action: str,
        objective: str,
    ) -> None:
        self._busy = True
        self._cancel_armed = False
        prompt.text = ""
        prompt.disabled = False
        prompt.read_only = False
        prompt.border_title = _PROMPT_RUNNING
        visible = objective if action == "create" else "继续当前 Goal"
        if objective and not self._first_user_text:
            self._first_user_text = objective
        self._append(
            Static(
                f"[bold cyan]Goal[/bold cyan]  {escape(visible)}",
                classes="user-turn",
            )
        )
        self._update_header("running")
        self.run_worker(
            self._do_goal_command(action, objective),
            name=f"goal_{action}",
            exclusive=False,
        )

    # 发送 Goal IPC 命令、更新顶栏状态并渲染可审计摘要
    async def _do_goal_command(self, action: str, value: str = "") -> None:
        if self._client is None or self._session_id is None:
            return
        method = f"goal.{action}"
        params: dict[str, object] = {"session_id": self._session_id}
        if action == "create":
            params["objective"] = value
        elif action == "edit":
            params["objective"] = value
        elif action == "complete":
            params["summary"] = value
        elif action == "status":
            method = "goal.get"
        try:
            result = await self._client.send_command(method, params)
            if action == "list":
                goals = result.get("goals", [])
                lines = [self._format_goal_summary(item) for item in goals]
                self._append(
                    Static(
                        "[bold cyan]Goals[/bold cyan]\n"
                        + ("\n".join(lines) if lines else "[dim]No goals.[/dim]"),
                        classes="log-line",
                    )
                )
            else:
                raw_goal = result.get("goal")
                if isinstance(raw_goal, dict):
                    self._apply_goal_state(raw_goal)
                    self._append(
                        Static(
                            "[bold cyan]Goal[/bold cyan]  "
                            + self._format_goal_summary(raw_goal),
                            classes="log-line",
                        )
                    )
                else:
                    self._active_goal_id = None
                    self._active_goal_status = ""
                    self._append(
                        Static("[dim]当前 session 没有未完成 Goal。[/dim]", classes="log-line")
                    )
                    self._update_header("running" if self._busy else "ready")
        except (IpcError, RuntimeError, OSError) as exc:
            self._append(Static(f"[red]goal error: {exc}[/red]", classes="log-line"))
            if action in {"create", "resume"}:
                self._busy = False
                self._update_header("ready")
                self._restore_ready_prompt()

    # 把 Goal 字典同步为 TUI 顶栏所需的最小状态
    def _apply_goal_state(self, goal: dict[str, Any]) -> None:
        status = str(goal.get("status", ""))
        self._active_goal_id = str(goal.get("id", "")) or None
        self._active_goal_status = status
        if status in {"completed", "cleared"}:
            self._active_goal_id = None
        self._update_header("running" if self._busy else "ready")

    # 生成单行 Goal 状态摘要供 status/list 共用
    def _format_goal_summary(self, goal: object) -> str:
        if not isinstance(goal, dict):
            return "[red]invalid goal payload[/red]"
        goal_id = escape(str(goal.get("id", "?")))
        status = escape(str(goal.get("status", "unknown")))
        status_color = {
            "active": "green",
            "paused": "yellow",
            "blocked": "red",
            "completed": "green",
            "cleared": "dim",
        }.get(status, "dim")
        objective = escape(str(goal.get("objective", "")))
        tokens_used = int(goal.get("tokens_used", 0) or 0)
        budget = goal.get("token_budget")
        usage = f"  tokens={tokens_used}/{budget}" if budget is not None else ""
        runs = len(goal.get("linked_run_ids", []) or [])
        criteria = len(goal.get("completion_criteria", []) or [])
        evidence = len(goal.get("completion_evidence", []) or [])
        current_run = str(goal.get("current_run_id") or "")
        progress = f"criteria={criteria} evidence={evidence}"
        running = f"  run={escape(current_run[:12])}" if current_run else ""
        reason = str(goal.get("status_reason") or "").strip()
        reason_line = f"\n[yellow]{escape(reason)}[/yellow]" if reason else ""
        return (
            f"[bold]{goal_id}[/bold]  [{status_color}]{status}[/{status_color}]  {objective}"
            f"\n[dim]runs={runs}  {progress}{usage}{running}[/dim]"
            f"{reason_line}"
        )

    # 查询当前 session 的未终结 Goal，供会话恢复后还原顶栏
    async def _refresh_goal_state(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "goal.get",
                {"session_id": self._session_id},
            )
        except (IpcError, RuntimeError, OSError):
            return
        raw_goal = result.get("goal")
        if isinstance(raw_goal, dict):
            self._apply_goal_state(raw_goal)
        else:
            self._active_goal_id = None
            self._active_goal_status = ""
            self._update_header("running" if self._busy else "ready")

    # 在 worker 中执行手动压缩命令，完成后显示结果横幅
    async def _do_compact(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._append(Static("[dim]compacting context...[/dim]", classes="log-line"))
        try:
            result = await ipc_actions.compact(self._client, self._session_id)
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
        except IpcActionError as exc:
            self._append(Static(f"[red]compact error: {exc}[/red]", classes="log-line"))

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
        result = await ipc_actions.get_authority_snapshot(
            self._client, self._session_id
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
                result = await ipc_actions.set_authority(
                    self._client,
                    self._session_id,
                    profile=profile.value,
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._authority_preset = preset
        except IpcActionError as exc:
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
                result = await ipc_actions.set_authority(
                    self._client,
                    self._session_id,
                    mode=mode.value,
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._input_runtime_mode = mode
        except IpcActionError as exc:
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
                result = await ipc_actions.set_authority(
                    self._client,
                    self._session_id,
                    workspace_trust=trust.value,
                )
                self._apply_authority_snapshot(dict(result.get("snapshot", {})))
            else:
                self._workspace_trust = trust
        except IpcActionError as exc:
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
        state = "ENFORCED" if available else "DEGRADED"
        color = "green" if available else "yellow"
        explanation = (
            "OS 强制隔离后端可用；每次命令的实际隔离计划与结果记录在 receipt 中。"
            if available
            else (
                "当前没有 OS 强制隔离；危险动作继续走 ASK 审批和工作区边界，"
                "但这些机制不等同于系统沙箱。"
            )
        )
        self._append(
            Static(
                f"[bold cyan]Sandbox[/bold cyan]  [{color}]{state}[/{color}]"
                f"  [dim]{kind}: {reason}[/dim]\n"
                f"[dim]{explanation}[/dim]",
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
            result = await ipc_actions.get_tasks(self._client, self._session_id)
            tasks = list(result)
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
        except IpcActionError as exc:
            self._append(Static(f"[red]tasks error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示全部持久 Worker/Fleet 状态、attempt、预算和简短结果
    async def _show_workers(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.get_workers(self._client)
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
        except IpcActionError as exc:
            self._append(Static(f"[red]workers error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示 MCP server 列表，或按名称展开单个 server 的工具清单
    async def _show_mcp(self, server_name: str = "") -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_mcp_servers(self._client)
            servers = list(result.get("servers", []))
            if server_name:
                target = next(
                    (
                        item
                        for item in servers
                        if str(item.get("name", "")) == server_name
                    ),
                    None,
                )
                body = (
                    render_mcp_tools(target)
                    if target is not None
                    else f"[dim]未找到 MCP server：{escape(server_name)}[/dim]"
                )
            else:
                body = render_mcp_servers(servers)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._append(Static(f"[red]mcp error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示引用状态、大小和 GC 候选 Artifact 清单
    async def _show_artifacts(self, days: int = 30) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_artifacts(self._client, days=days)
            self._append(Static(render_artifacts(result), classes="log-line"))
        except IpcActionError as exc:
            self._append(Static(f"[red]artifacts error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 预览或确认执行引用感知 Artifact GC 并展示审计 receipt
    async def _gc_artifacts(self, days: int, *, confirmed: bool) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.gc_artifacts(
                self._client,
                days=days,
                confirmed=confirmed,
            )
            self._append(Static(render_artifact_gc(result), classes="log-line"))
        except IpcActionError as exc:
            self._append(Static(f"[red]artifact GC error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示 hook 配置表与最近执行记录
    async def _show_hooks(self) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_hooks(self._client)
            body = render_hooks(result)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._append(Static(f"[red]hooks error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 手动重跑指定 hook 并在滚动区展示本次执行状态
    async def _do_hook_rerun(self, hook_id: str) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.rerun_hook(self._client, hook_id)
            status = str(result.get("status", ""))
            reason = str(result.get("reason", "")).strip()
            detail = f"  [dim]{escape(reason)}[/dim]" if reason else ""
            self._append(
                Static(
                    f"[green]hook {escape(hook_id)} rerun[/green]  "
                    f"[dim]{escape(status)}[/dim]{detail}",
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._append(Static(f"[red]hook rerun error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示当前项目记忆条目
    async def _show_memory(self) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_memories(self._client)
            memories = list(result.get("memories", []))
            body = render_memory(memories)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._append(Static(f"[red]memory error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 删除指定项目记忆并在滚动区回显结果
    async def _do_memory_delete(self, memory_id: str) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.delete_memory(self._client, memory_id)
            if result.get("deleted"):
                self._append(
                    Static(
                        f"[green]已删除记忆 {escape(memory_id)}[/green]",
                        classes="log-line",
                    )
                )
            else:
                self._append(
                    Static(
                        f"[yellow]未找到记忆 {escape(memory_id)}[/yellow]",
                        classes="log-line",
                    )
                )
        except IpcActionError as exc:
            self._append(Static(f"[red]memory delete error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示后台任务中心：bg 任务列表 + 子代理汇总，或单个任务全量输出
    async def _show_jobs(self, show_id: str = "") -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.get_background(
                self._client, job_id=show_id
            )
            jobs = list(result.get("jobs", []))
            if show_id:
                body = render_job_output(jobs)
            else:
                workers = await ipc_actions.get_workers(self._client)
                worker_records = list(workers.get("workers", []))
                body = (
                    render_jobs(jobs)
                    + "\n\n"
                    + render_workers_summary(worker_records)
                )
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._append(Static(f"[red]jobs error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 取消后台任务（bg-* 或 Worker/子代理）并在滚动区回显结果
    async def _do_job_cancel(self, job_id: str) -> None:
        if self._client is None:
            return
        try:
            if job_id.startswith("bg-"):
                await ipc_actions.cancel_background(self._client, job_id)
            else:
                result = await ipc_actions.cancel_worker(self._client, job_id)
                status = str(result.get("status", ""))
                self._append(
                    Static(
                        f"[green]cancelled {escape(job_id)}[/green]  "
                        f"[dim]{escape(status)}[/dim]",
                        classes="log-line",
                    )
                )
                return
            self._append(
                Static(
                    f"[green]cancelled {escape(job_id)}[/green]",
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._append(Static(f"[red]cancel job error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 从 Core durable ledger 加载 workflow 列表或单个 Work Graph projection
    async def _show_workflow(self, workflow_id: str = "") -> None:
        if self._client is None:
            return
        try:
            if workflow_id:
                result = await ipc_actions.get_workflow(
                    self._client, workflow_id
                )
                body = render_workflow_graph(dict(result.get("workflow", {})))
            else:
                result = await ipc_actions.list_workflows(self._client)
                body = render_workflow_list(list(result.get("workflows", [])))
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
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
            result = await ipc_actions.start_workflow(
                self._client,
                source,
                format_name,
            )
            workflow_id = escape(str(result.get("workflow_id", "")))
            self._append(
                Static(
                    f"[green]Workflow 已启动[/green] {workflow_id}",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError) as exc:
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
            result = await ipc_actions.get_diff(self._client)
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
        except (IpcActionError, ValueError, TypeError) as exc:
            self._append(Static(f"[red]diff error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载最近一次 run 的可恢复 checkpoint 并打开选择器
    async def _show_rewind_picker(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            checkpoints = [
                dict(item)
                for item in await ipc_actions.list_checkpoints(
                    self._client, self._session_id
                )
                if item.get("status") == "ready"
            ]
            if not checkpoints:
                self._append(
                    Static("[dim]当前会话没有可恢复的 checkpoint。[/dim]", classes="log-line")
                )
                self._restore_ready_prompt()
                return
            self.mount(CheckpointPicker(checkpoints), before="#prompt")
        except IpcActionError as exc:
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
            result = await ipc_actions.rewind(
                self._client,
                self._session_id,
                message.checkpoint_id,
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
        except IpcActionError as exc:
            self._append(Static(f"[red]rewind error: {exc}[/red]", classes="log-line"))
        finally:
            self._restore_ready_prompt()

    # 加载并展示当前会话上下文估算和最近一次真实模型占用率
    async def _show_context(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.get_context(self._client, self._session_id)
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
        except (IpcActionError, ValueError, TypeError) as exc:
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
                context = await ipc_actions.get_context(
                    self._client, self._session_id
                )
                resolved_id = str(context.get("last_run_id") or "")
            if not resolved_id:
                raise ValueError("current session has no turn")
            result = await ipc_actions.inspect_turn(self._client, resolved_id)
            self._append(
                Static(render_turn_inspector(dict(result)), classes="log-line")
            )
        except (IpcActionError, ValueError, TypeError) as exc:
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
            route = self._configuration.set_active(route_id)
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
        self._configuration.save_route(updated, update=True)
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
    async def _save_config_route(
        self,
        provider: ProviderPreset,
        api_key: str,
        model: str,
    ) -> None:
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
        route = ProviderRoute.model_validate(
            {
                "id": provider.id,
                "provider": provider_kind,
                "wire_format": wire_format,
                "base_url": provider.chat_url or "https://api.anthropic.com",
                "model": model,
                "credential_ref": f"file:{provider.id}",
                "supports_prompt_cache": provider.anthropic_api,
            }
        )
        exists = any(item.id == route.id for item in self._route_store.list())
        try:
            route = await self._configuration.save_route_checked(
                route,
                secret=api_key,
                activate=True,
                update=exists,
                doctor=self._provider_doctor,
            )
        except ConfigurationValidationError as exc:
            self._append(
                Static(
                    f"[red]Provider 验证失败[/red]  {escape(str(exc))}",
                    classes="log-line",
                )
            )
            self._restore_ready_prompt()
            return
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
            await self._save_config_route(
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
        await self._refresh_goal_state()
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
        *,
        attachments: list[dict[str, object]] | None = None,
    ) -> None:
        if self._client is None:
            return
        try:
            params: dict[str, object] = {
                "session_id": self._session_id or "",
                "content": content,
                "runtime_mode": runtime_mode.value,
            }
            if attachments:
                params["attachments"] = attachments
            await self._client.send_command(
                "session.send_message",
                params,
            )
        except (IpcError, RuntimeError, OSError) as e:
            for attachment in attachments or []:
                if attachment not in self._pending_image_attachments:
                    self._pending_image_attachments.append(attachment)
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
                        {
                            "tool_use_id": tool_use_id,
                            "decision": decision,
                            "selected_hunks": msg.selected_hunks,
                            "patch_plan_id": msg.patch_plan_id,
                        },
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
        goal = (
            f"  [bold cyan]goal:{escape(self._active_goal_status)}[/bold cyan]"
            if self._active_goal_id is not None
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
            f"  [dim]repo:{escape(self._workspace.name)}[/dim]"
            f"{session}{model}{mode}{permission}{trust}{ctx}{cost}{goal}"
            f"  [{color}]{state}[/{color}]"
        )

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            render_event(self, event)
            if (
                event.get("type") in {"session.waiting_for_input", "session.interrupted"}
                and event.get("session_id") == self._session_id
            ):
                self.call_later(self._refresh_goal_state)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))


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

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

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
from code_rook.core.features import labs_enabled
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
from code_rook.core.llm.routes import ProviderRoute, get_route_preset
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
    command_category,
    command_palette_direct,
    command_palette_priority,
    complete_command_arg_text,
    match_slash_command,
    visible_slash_commands,
)
from code_rook.tui.connection import TuiConnection
from code_rook.tui.ipc_actions import IpcActionError
from code_rook.tui.panels import (
    ChangeCenterOverlay,
    ChangeCenterPanel,
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
from code_rook.tui.product import (
    ReadinessCard,
    RunEvidenceReducer,
    RunResultCard,
    SafeErrorCard,
    detect_locale,
    diagnostic_id,
    normalize_locale,
    save_locale,
    tr,
)
from code_rook.tui.render import render_event
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
    _tool_failure_text as _tool_failure_text,
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
    _clear_input_history,
    _input_history_enabled,
    _input_history_path,
    _load_input_history,
    _set_input_history_enabled,
)
from code_rook.tui.widgets.input import (
    _save_input_history_entry as _save_input_history_entry,
)
from code_rook.tui.widgets.palette import CommandPalette, CommandPaletteItem
from code_rook.tui.widgets.permission import (
    _MODE_CYCLE,
    _PERMISSION_PRESETS,
    PermissionBlock,
    PermissionModePicker,
    PermissionSelect,
    permission_presets,
)
from code_rook.tui.widgets.pickers import CheckpointPicker, PlanReview, UserQuestionSelect
from code_rook.tui.widgets.selectors import ModelPicker, ProviderPicker, SessionPicker
from code_rook.tui.widgets.stream import LLMStreamBlock, ToolCallBlock, ToolStepGroup

log = logging.getLogger(__name__)

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
        Binding("ctrl+p", "command_palette", "command palette", show=False, priority=True),
        Binding("ctrl+o", "toggle_details", "toggle details", show=False, priority=True),
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
    Screen { background: $background; layers: base overlay; }
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
    Static.product-card {
        height: auto;
        margin: 1 2 0 2;
        padding: 1 2;
        border: round #3b4654;
        background: #17191d;
    }
    Static.error-card { border: round #78434a; }
    Static.readiness-card { border: round #73652e; }
    Static.result-card { border: round #386a70; }
    Static.permission-pending { display: none; }
    #attachment-strip {
        display: none;
        height: auto;
        max-height: 3;
        padding: 0 2;
        background: #1d232a;
        color: #72c7d4;
    }
    #status-bar {
        height: 1;
        padding: 0 2;
        background: #171b20;
        color: #8d98a5;
    }
    Screen.high-contrast #header, Screen.high-contrast #status-bar {
        background: black;
        color: white;
        text-style: bold;
    }
    Markdown.history-assistant { color: $text; }
    """

    # 初始化连接参数和 TUI 内部状态
    def __init__(
        self,
        host: str,
        port: int,
        replay_run_id: str | None = None,
        resume_session_id: str | None = None,
        continue_recent: bool = True,
        auth_token: str | None = None,
        provider: str = "",
        model: str = "",
        models: list[str] | None = None,
        route: str = "",
        route_store: RouteStore | None = None,
        credential_store: CredentialStore | None = None,
        provider_doctor: ProviderDoctor | None = None,
        core_recovery: Callable[[], object] | None = None,
        locale: str | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._port = port
        self._workspace = Path.cwd().resolve()
        self._locale = normalize_locale(locale) if locale else detect_locale()
        self._theme_mode = "auto"
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
        self._input_history_enabled = _input_history_enabled(self._workspace)
        self._input_history_path = _input_history_path(self._workspace)
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
        self._run_phase = "ready"
        self._run_phase_current = 0
        self._run_phase_total = 0
        self._queued_messages: deque[str] = deque()
        self._details_expanded = False
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
        self._plan_run_id: str | None = None
        self._plan_request = ""
        self._pending_question_id: str | None = None
        self._answering_question = False
        self._slash_items: list[CompletionItem] = []
        self._artifact_store = ArtifactStore(Path.cwd() / ".coderook" / "artifacts")
        self._pending_image_attachments: list[dict[str, object]] = []
        self._session_composer_states: dict[str, dict[str, object]] = {}
        self._session_transition_lock = asyncio.Lock()
        self._subagent_run_ids: dict[str, str] = {}  # child run_id -> description
        self._subagent_start_times: dict[str, float] = {}  # child run_id -> start time
        self._product_notice_keys: set[str] = set()
        self._run_evidence = RunEvidenceReducer()
        self._rendered_result_runs: set[str] = set()
        self._rendered_result_order: deque[str] = deque()
        self._pending_result_runs: set[str] = set()
        self._deferred_result_events: dict[str, dict[str, dict[str, Any]]] = {}
        self._change_state_digest = ""
        self._change_review_scope = ""
        self._pending_rewind: dict[str, str] | None = None
        self._labs_enabled = labs_enabled()

    def compose(self) -> ComposeResult:
        yield Label("[bold]CodeRook[/bold]", id="header")
        yield VerticalScroll(id="log-view")
        yield Static("", id="attachment-strip")
        yield Static("", id="status-bar")
        yield ChatTextArea(id="prompt", show_line_numbers=False)

    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._append(Static(self._render_banner(), id="banner"))
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.set_history(
            _load_input_history() if self._input_history_enabled else [],
            path=self._input_history_path,
            enabled=self._input_history_enabled,
        )
        prompt.disabled = True
        prompt.border_title = tr("shell.connecting", self._locale)
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
                prompt.border_title = tr("shell.review_plan", self._locale)
            elif self._input_runtime_mode == RuntimeMode.PLAN:
                prompt.border_title = tr("shell.plan", self._locale)
            else:
                prompt.border_title = tr("shell.connected", self._locale)
                prompt.focus()
        self._update_header("plan ready" if self._plan_review_pending else "ready")
        self._show_startup_state()

    # 连接断开后：禁用输入框并提示正在重试
    def _mark_disconnected(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = True
            prompt.read_only = False
            prompt.border_title = tr("shell.disconnected", self._locale)

    # 向 transcript 追加一次去重的产品状态说明，避免重连循环刷屏
    def _show_product_notice(self, key: str, message: str) -> None:
        if key in self._product_notice_keys:
            return
        self._product_notice_keys.add(key)
        self._append(Static(message, classes="log-line"))

    # 追加一次去重的产品控件，供状态卡与错误卡共享
    def _show_product_widget(self, key: str, widget: Widget) -> None:
        if key in self._product_notice_keys:
            return
        self._product_notice_keys.add(key)
        self._append(widget)

    # 记录完整异常并只向用户展示类别、恢复动作与诊断编号
    def _show_safe_error(
        self,
        category: str,
        error: BaseException | str | None = None,
        *,
        action: str | None = None,
        notice_key: str | None = None,
    ) -> str:
        identifier = diagnostic_id(category, error)
        log.error(
            "TUI operation failed diagnostic_id=%s category=%s detail=%s",
            identifier,
            category,
            error,
        )
        card = SafeErrorCard(category, identifier, action=action, locale=self._locale)
        if notice_key is None:
            self._append(card)
        else:
            self._show_product_widget(notice_key, card)
        return identifier

    # 展示连接问题及用户可执行的恢复动作
    def _show_connection_problem(self, kind: str, detail: str) -> None:
        if kind == "recovering":
            self._show_product_notice(
                "connection:recovering",
                f"[bold green]{tr('app.core.recovered', self._locale)}[/bold green]",
            )
            return
        action = (
            "authentication"
            if kind == "authentication"
            else ("protocol" if kind == "protocol" else "connection")
        )
        self._show_safe_error(
            kind,
            detail,
            action=action,
            notice_key=f"connection:{kind}",
        )

    # 显示会话新建、首次恢复或断线续接结果，让用户明确当前上下文来源
    def _show_session_ready(
        self,
        action: str,
        session_id: str,
        title: str,
        history_count: int | None,
    ) -> None:
        label = tr(f"app.session.{action}", self._locale)
        if label == f"app.session.{action}":
            label = tr("app.session.ready", self._locale)
        short_id = escape(session_id)
        title_text = f"  [bold]{escape(title)}[/bold]" if title else ""
        history_text = ""
        if history_count is not None:
            history = tr("app.session.history", self._locale, count=history_count)
            history_text = f"  [dim]{history}[/dim]"
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
        try:
            readiness = self._configuration.readiness()
        except (OSError, ValueError, RouteStoreError, SystemExit) as exc:
            self._show_safe_error(
                "configuration",
                exc,
                notice_key="startup:configuration-error",
            )
        else:
            if not readiness.local_ready:
                self._show_product_widget(
                    f"startup:readiness:{readiness.status}:{readiness.route_id or '-'}",
                    ReadinessCard(readiness, locale=self._locale),
                )
        if not bool(self._sandbox.get("available", False)):
            kind = escape(str(self._sandbox.get("kind", "none")))
            reason = escape(str(self._sandbox.get("reason", "unavailable")))
            self._show_product_notice(
                "startup:degraded-sandbox",
                "[bold yellow]Sandbox DEGRADED[/bold yellow]  "
                f"[dim]{kind}: {reason}[/dim]\n"
                f"[dim]{tr('app.startup.sandbox_degraded', self._locale)}[/dim]",
            )
        if self._labs_enabled:
            self._show_product_notice(
                "startup:labs-enabled",
                f"[bold yellow]{tr('app.startup.labs', self._locale)}[/bold yellow]",
            )

    # 提交执行任务前调用统一 readiness；未就绪时保留草稿且不创建 run
    async def _ensure_task_ready(self) -> bool:
        try:
            readiness = await self._configuration.probe_readiness(
                doctor=self._provider_doctor,
            )
            active_route = self._route_store.active()
        except (OSError, ValueError, RouteStoreError, SystemExit) as exc:
            self._show_safe_error("configuration", exc)
            return False
        if not readiness.local_ready:
            self._show_product_widget(
                f"submit:readiness:{readiness.status}:{readiness.route_id or '-'}",
                ReadinessCard(readiness, locale=self._locale),
            )
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.border_title = tr("readiness.prompt", self._locale)
                prompt.focus()
            return False
        self._route = str(readiness.route_id or "")
        self._model = str(readiness.model or "")
        if active_route is not None:
            self._provider = active_route.catalog_id or active_route.id
        else:
            self._provider = str(readiness.provider or "")
        return True

    # 查看、开关或清空当前工作区输入历史
    def _handle_history_command(self, argument: str) -> None:
        action = argument.strip().casefold() or "status"
        prompt = self._prompt()
        if action == "on":
            self._input_history_enabled = True
            _set_input_history_enabled(True, self._workspace)
            if prompt is not None:
                prompt.set_history_enabled(True)
            message = tr("history.on", self._locale)
        elif action == "off":
            self._input_history_enabled = False
            _set_input_history_enabled(False, self._workspace)
            if prompt is not None:
                prompt.set_history_enabled(False)
            message = tr("history.off", self._locale)
        elif action == "clear":
            _clear_input_history(self._workspace)
            if prompt is not None:
                prompt.clear_history()
            message = tr("history.clear", self._locale)
        elif action == "status":
            status_key = "history.enabled" if self._input_history_enabled else "history.disabled"
            message = tr(
                "history.status",
                self._locale,
                status=tr(status_key, self._locale),
            )
        else:
            message = tr("history.usage", self._locale)
        self._append(Static(escape(message), classes="log-line"))

    # 查看或持久切换界面语言，当前运行中的事件状态不受影响
    def _handle_language_command(self, argument: str) -> None:
        requested = argument.strip()
        if not requested:
            message = tr("language.current", self._locale, language=self._locale)
        elif requested not in {"zh-CN", "en-US"}:
            message = tr("language.usage", self._locale)
        else:
            self._locale = save_locale(requested)
            message = tr("language.changed", self._locale, language=self._locale)
            self._refresh_locale_ui()
        self._append(Static(escape(message), classes="log-line"))

    # 查看或应用 auto/dark/light/high-contrast 主题并立即刷新固定壳层
    def _handle_theme_command(self, argument: str) -> None:
        requested = argument.strip().casefold()
        if not requested:
            message = f"theme: {self._theme_mode}"
        elif requested not in {"auto", "dark", "light", "high-contrast"}:
            message = "usage: /theme auto|dark|light|high-contrast"
        else:
            self._theme_mode = requested
            selected = "textual-light" if requested == "light" else "textual-dark"
            if requested == "auto":
                selected = "textual-dark"
            self.theme = selected
            try:
                self.screen.set_class(requested == "high-contrast", "high-contrast")
            except Exception:
                pass
            message = f"theme: {requested}"
            self._update_header(self._header_state)
        self._append(Static(escape(message), classes="log-line"))

    # 渲染当前语言的启动品牌与快捷入口提示
    def _render_banner(self) -> str:
        hint = escape(tr("shell.banner_hint", self._locale))
        if self._locale == "zh-CN":
            title = "CodeRook 已就绪"
            suggestions = (
                "解释这个仓库的核心架构",
                "检查当前未提交变更",
                "定位最相关的测试并运行",
            )
        else:
            title = "CodeRook is ready"
            suggestions = (
                "Explain this repository's architecture",
                "Review the current uncommitted changes",
                "Find and run the most relevant tests",
            )
        lines = [f"[bold cyan]{title}[/bold cyan]", f"[dim]{hint}[/dim]"]
        lines.extend(f"  [cyan]›[/cyan] {escape(item)}" for item in suggestions)
        return "\n".join(lines)

    # 根据当前运行状态选择 composer 的即时本地化标题
    def _localized_prompt_title(self) -> str:
        if self._client is None:
            key = (
                "shell.disconnected"
                if self._header_state == "disconnected"
                else "shell.connecting"
            )
        elif self._plan_review_pending:
            key = "shell.review_plan"
        elif self._busy:
            key = (
                "shell.planning"
                if self._input_runtime_mode == RuntimeMode.PLAN
                else "shell.running"
            )
        elif self._input_runtime_mode == RuntimeMode.PLAN:
            key = "shell.plan"
        else:
            key = "shell.connected"
        return tr(key, self._locale)

    # 语言切换后刷新顶栏、composer、补全、品牌、附件条及已打开 overlay
    def _refresh_locale_ui(self) -> None:
        self._slash_items = self._build_slash_items()
        try:
            self.query_one("#banner", Static).update(self._render_banner())
        except Exception:
            pass
        prompt = self._prompt()
        if prompt is not None:
            prompt.border_title = self._localized_prompt_title()
        for widget in self.query(Widget):
            setter = getattr(widget, "set_locale", None)
            if callable(setter):
                setter(self._locale)
        self._refresh_attachment_strip()
        self._update_header(self._header_state)

    # 返回当前语言的命令参数提示，无翻译时保留技术语法
    def _command_usage(self, name: str, fallback: str) -> str:
        key = f"usage.{name}"
        localized = tr(key, self._locale)
        return fallback if localized == key else localized

    # 构建斜杠命令候选列表：内建命令（含 usage）+ 所有已注册 skill（无 usage）
    def _build_slash_items(self) -> list[CompletionItem]:
        items: list[CompletionItem] = [
            CompletionItem(
                cmd.name,
                tr(f"command.{cmd.name}", self._locale),
                self._command_usage(cmd.name, cmd.usage),
            )
            for cmd in visible_slash_commands(labs_enabled=self._labs_enabled)
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

    # 构建按产品类别排序的本地化 Ctrl+P 命令候选
    def _build_palette_items(self) -> list[CommandPaletteItem]:
        return [
            CommandPaletteItem(
                command=command.name,
                description=tr(f"command.{command.name}", self._locale),
                category=command_category(command.name),
                usage=self._command_usage(command.name, command.usage),
                direct=command_palette_direct(command.name),
                priority=command_palette_priority(command.name),
            )
            for command in visible_slash_commands(labs_enabled=self._labs_enabled)
        ]

    # 打开或关闭可聚焦的分类命令面板
    def action_command_palette(self) -> None:
        try:
            palette = self.query_one(CommandPalette)
        except NoMatches:
            palette = CommandPalette(self._build_palette_items(), locale=self._locale)
            self.mount(palette)
            return
        palette.remove()
        prompt = self._prompt()
        if prompt is not None and not prompt.disabled:
            prompt.focus()

    # 统一展开或收起推理、步骤与已完成工具详情，保持默认时间线紧凑
    def action_toggle_details(self) -> None:
        self._details_expanded = not self._details_expanded
        for block in self.query(LLMStreamBlock):
            if "answer" not in block.classes:
                block.set_class(not self._details_expanded, "collapsed")
        for group in self.query(ToolStepGroup):
            group.set_class(not self._details_expanded, "collapsed")
        for tool in self.query(ToolCallBlock):
            tool.set_expanded(self._details_expanded)

    # 复制失败工具的完整错误内容并给出就地反馈
    def on_tool_call_block_copy_requested(
        self,
        message: ToolCallBlock.CopyRequested,
    ) -> None:
        if self._write_clipboard(message.block._output):
            self.notify(
                "错误已复制" if self._locale != "en-US" else "Error copied"
            )

    # 把安全重试建议写入输入框供用户确认，避免直接重复底层副作用
    def on_tool_call_block_retry_requested(
        self,
        message: ToolCallBlock.RetryRequested,
    ) -> None:
        prompt = self._prompt()
        if prompt is None:
            return
        retry = message.block.retry_prompt()
        prompt.text = f"{prompt.text.rstrip()}\n{retry}".lstrip()
        prompt.move_cursor(prompt.document.end)
        if not prompt.disabled:
            prompt.focus()
        self.notify(
            "重试建议已填入输入框"
            if self._locale != "en-US"
            else "Retry guidance added to the composer"
        )

    # 在工作区边界内以有界纯文本预览工具关联文件
    def on_tool_call_block_open_requested(
        self,
        message: ToolCallBlock.OpenRequested,
    ) -> None:
        raw = message.block.primary_location()
        try:
            candidate = Path(raw)
            target = (
                candidate.resolve()
                if candidate.is_absolute()
                else (self._workspace / candidate).resolve()
            )
            relative = target.relative_to(self._workspace)
            if not target.is_file():
                raise ValueError("location is not a file")
            content = target.read_text(encoding="utf-8", errors="replace")
            truncated = len(content) > 8_000
            preview = content[:8_000]
            suffix = "\n… [preview truncated]" if truncated else ""
            self._append(
                Static(
                    f"{relative.as_posix()}\n\n{preview}{suffix}",
                    classes="log-line",
                    markup=False,
                )
            )
        except (OSError, ValueError):
            self.notify(
                "只能预览当前工作区内的文本文件"
                if self._locale != "en-US"
                else "Only text files inside the workspace can be previewed",
                severity="warning",
            )

    # 关闭 Ctrl+P 面板并把焦点还给 composer
    def on_command_palette_dismissed(self, message: CommandPalette.Dismissed) -> None:
        message.palette.remove()
        prompt = self._prompt()
        if prompt is not None and not prompt.disabled:
            prompt.focus()

    # 选择命令后直接执行安全无参数入口，其他命令写入 composer 等待补参
    async def on_command_palette_selected(self, message: CommandPalette.Selected) -> None:
        message.palette.remove()
        prompt = self._prompt()
        if prompt is None:
            return
        content = f"/{message.item.command}"
        if message.item.direct:
            command = match_slash_command(content)
            if command is not None:
                await command.handler(self, prompt, content)
            return
        prompt.text = f"{content} "
        prompt.move_cursor(prompt.document.end)
        if not prompt.disabled:
            prompt.focus()

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
            popup = SlashCompleteWidget(self._slash_items, locale=self._locale)
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
                    f"[yellow]{tr('app.permission.busy', self._locale)}[/yellow]",
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
                    f"[yellow]{tr('app.mode.busy', self._locale)}[/yellow]",
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
                    f"[yellow]{tr('app.cancel.confirm', self._locale)}[/yellow]",
                    classes="log-line",
                )
            )
            return
        run_id = self._active_run_id
        self._cancel_requested = True
        self._append(
            Static(
                f"[yellow]{tr('app.cancel.running', self._locale, run_id=run_id)}[/yellow]",
                classes="log-line",
            )
        )
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
            self.notify(tr("app.copy.empty", self._locale), severity="warning")
            return False
        self.notify(tr("app.copy.done", self._locale))
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
            ("Enter", tr("help.enter", self._locale)),
            ("↑ / ↓", tr("help.history", self._locale)),
            ("Tab", tr("help.mode", self._locale)),
            ("Shift+Tab", tr("help.permission", self._locale)),
            ("Ctrl+C", tr("help.cancel", self._locale)),
            ("Ctrl+Shift+C", tr("help.copy", self._locale)),
            ("Ctrl+End", tr("help.scroll", self._locale)),
            ("Ctrl+P", tr("help.palette", self._locale)),
            ("Ctrl+Q", tr("help.quit", self._locale)),
        ]
        lines = [f"[bold cyan]{escape(tr('help.keys', self._locale))}[/bold cyan]"]
        for key, desc in keys:
            lines.append(f"  [bold]{key}[/bold]  {escape(desc)}")
        lines.append(f"[bold cyan]{escape(tr('help.commands', self._locale))}[/bold cyan]")
        for item in self._slash_items:
            lines.append(f"  [cyan]/{item.name}[/cyan]  [dim]{escape(item.description)}[/dim]")
        lines.append(f"[dim]{escape(tr('help.footer', self._locale))}[/dim]")
        self._append(Static("\n".join(lines), classes="log-line"))

    # 在 TUI 本地执行 /skills list/show/install/remove/audit，并要求变更操作显式确认
    def _handle_skills_command(self, content: str) -> None:
        try:
            parts = shlex.split(content)
        except ValueError as exc:
            self._append(
                Static(
                    "[red]"
                    + tr(
                        "app.skills.invalid",
                        self._locale,
                        error=escape(str(exc)),
                    )
                    + "[/red]",
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
                        f"[bold cyan]{tr('app.skills.title', self._locale)}[/bold cyan]\n"
                        + escape(
                            "\n".join(lines)
                            or tr("app.skills.empty", self._locale)
                        ),
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
                        f"[bold cyan]{tr('app.skills.manifest', self._locale)}[/bold cyan]\n"
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
                installed_message = tr(
                    "app.skills.installed",
                    self._locale,
                    name=escape(installed.name),
                )
                self._append(
                    Static(
                        f"[green]{installed_message}[/green]",
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
                removed = tr(
                    "app.skills.removed",
                    self._locale,
                    scope=scope,
                    name=escape(args[0]),
                )
                self._append(
                    Static(
                        f"[green]{removed}[/green]",
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
                        f"[bold cyan]{tr('app.skills.audit', self._locale)}[/bold cyan]\n"
                        + escape(
                            "\n".join(lines)
                            or tr("app.skills.empty", self._locale)
                        ),
                        classes="log-line",
                    )
                )
                return
            raise SkillManagerError(tr("app.skills.usage", self._locale))
        except SkillConfirmationRequired as exc:
            preview = json.dumps(exc.preview.model_dump(mode="json"), ensure_ascii=False, indent=2)
            self._append(
                Static(
                    f"[bold yellow]{tr('app.skills.preview', self._locale)}[/bold yellow]\n"
                    + escape(preview)
                    + f"\n[dim]{tr('app.skills.confirm', self._locale)}[/dim]",
                    classes="log-line",
                )
            )
        except (SkillManagerError, OSError) as exc:
            self._show_safe_error("skills", exc)

    async def _do_cancel_run(self, run_id: str) -> None:
        if self._client is None:
            return
        try:
            await self._client.send_command("run.cancel", {"run_id": run_id})
        except (IpcError, RuntimeError, OSError) as exc:
            self._cancel_requested = False
            self._show_safe_error("submission", exc)

    # 将输入框提交内容发送给当前 chat session；用 worker 发送，避免 await 阻塞 App 消息泵
    async def on_chat_text_area_submitted(self, event: ChatTextArea.Submitted) -> None:
        content = event.value.strip()
        if not content and self._pending_image_attachments:
            content = tr("app.submit.image_default", self._locale)
        if not content:
            return
        if self._pending_question_id is not None and self._answering_question:
            event.text_area.record_history(content)
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
        if cmd is not None and cmd.labs and not self._labs_enabled:
            event.text_area.text = ""
            self._append(
                Static(
                    f"[bold yellow]{tr('app.labs.disabled', self._locale)}[/bold yellow]",
                    classes="log-line",
                )
            )
            return
        if cmd is not None and cmd.name == "goal":
            argument = content.removeprefix("/goal").strip()
            action = argument.partition(" ")[0] if argument else "status"
            if (
                action
                not in {
                    "status",
                    "list",
                    "pause",
                    "edit",
                    "complete",
                    "clear",
                }
                and not await self._ensure_task_ready()
            ):
                return
            event.text_area.record_history(content)
            await cmd.handler(self, event.text_area, content)
            return
        if self._busy:
            if self._client is None or self._active_run_id is None:
                self._append(
                    Static(
                        f"[yellow]{tr('app.submit.starting', self._locale)}[/yellow]",
                        classes="log-line",
                    )
                )
                return
            event.text_area.record_history(content)
            event.text_area.text = ""
            queue_prefixes = ("queue:", "queue：", "排队:", "排队：")
            if content.casefold().startswith(queue_prefixes):
                queued = content.split(":", 1)[-1].split("：", 1)[-1].strip()
                if queued:
                    self._queued_messages.append(queued)
                    self._append(
                        Static(
                            f"[bold blue]queue >[/bold blue] {escape(queued)}",
                            classes="user-turn",
                        )
                    )
                    self._update_status_bar()
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
            goal_argument = content.removeprefix("/goal").strip()
            goal_action = goal_argument.partition(" ")[0] if goal_argument else "status"
            starts_run = (
                (cmd.name == "plan" and content != "/plan")
                or (cmd.name == "review")
                or (
                    cmd.name == "goal"
                    and goal_action
                    not in {
                        "status",
                        "list",
                        "pause",
                        "edit",
                        "complete",
                        "clear",
                    }
                )
            )
            if starts_run and not await self._ensure_task_ready():
                return
            event.text_area.record_history(content)
            await cmd.handler(self, event.text_area, content)
            return
        if self._client is None or self._session_id is None or self._busy:
            self._append(
                Static(
                    f"[yellow]{tr('app.submit.busy', self._locale)}[/yellow]",
                    classes="log-line",
                )
            )
            return
        if not await self._ensure_task_ready():
            return
        event.text_area.record_history(content)
        visible_content = content
        content = self._prepare_model_content(visible_content)
        self._begin_message(
            event.text_area,
            content,
            self._input_runtime_mode,
            visible_content=visible_content,
        )

    # 收到输入框图片粘贴后异步校验并写入内容寻址 ArtifactStore
    async def on_chat_text_area_image_pasted(
        self,
        event: ChatTextArea.ImagePasted,
    ) -> None:
        if len(self._pending_image_attachments) >= 8:
            self.notify(tr("attachments.limit", self._locale), severity="warning")
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
                raise ValueError(tr("attachments.too_large", self._locale))
            metadata = inspect_image(data)
            reference = await self._artifact_store.put(
                data,
                media_type=metadata.media_type,
            )
        except (ArtifactError, OSError, ValueError) as exc:
            self._show_safe_error("attachment", exc)
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
        self._refresh_attachment_strip()
        index = self._pending_image_attachments.index(attachment) + 1
        self._append(
            Static(
                "[cyan]"
                + escape(
                    tr(
                        "attachments.added",
                        self._locale,
                        index=index,
                        width=metadata.width,
                        height=metadata.height,
                        digest=reference.sha256[:12],
                    )
                )
                + "[/cyan] "
                + f"[dim]{escape(path.name)} · {escape(metadata.media_type)}[/dim]",
                classes="log-line",
            )
        )

    # 把附件字节数格式化为 composer 附件条使用的紧凑单位
    @staticmethod
    def _format_attachment_size(value: object) -> str:
        size = int(value) if isinstance(value, int) and not isinstance(value, bool) else 0
        if size < 1024:
            return f"{size} B"
        return f"{size / 1024:.1f} KiB"

    # 将附件元数据中的尺寸字段安全收窄为非负整数
    @staticmethod
    def _attachment_dimension(value: object) -> int:
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        return 0

    # 刷新 composer 上方持久附件条与输入框附件计数
    def _refresh_attachment_strip(self) -> None:
        try:
            strip = self.query_one("#attachment-strip", Static)
        except Exception:
            return
        attachments = self._pending_image_attachments
        if not attachments:
            strip.update("")
            strip.styles.display = "none"
            prompt = self._prompt()
            if prompt is not None:
                prompt.border_subtitle = ""
            return
        lines = [f"[bold]{escape(tr('attachments.title', self._locale))}[/bold]"]
        for index, attachment in enumerate(attachments, start=1):
            lines.append(
                escape(
                    tr(
                        "attachments.item",
                        self._locale,
                        index=index,
                        width=self._attachment_dimension(attachment.get("width")),
                        height=self._attachment_dimension(attachment.get("height")),
                        size=self._format_attachment_size(attachment.get("size")),
                        digest=str(attachment.get("sha256", ""))[:12],
                    )
                )
            )
        strip.update("  ".join(lines))
        strip.styles.display = "block"
        prompt = self._prompt()
        if prompt is not None:
            prompt.border_subtitle = tr(
                "shell.prompt.attachments",
                self._locale,
                count=len(attachments),
            )

    # 处理附件查看、按序号移除和清空动作
    def _handle_attachments_command(self, argument: str) -> None:
        parts = argument.split()
        message = ""
        if not parts:
            if not self._pending_image_attachments:
                message = tr("attachments.empty", self._locale)
            else:
                lines = [tr("attachments.title", self._locale)]
                for index, attachment in enumerate(self._pending_image_attachments, start=1):
                    lines.append(
                        tr(
                            "attachments.item",
                            self._locale,
                            index=index,
                            width=self._attachment_dimension(attachment.get("width")),
                            height=self._attachment_dimension(attachment.get("height")),
                            size=self._format_attachment_size(attachment.get("size")),
                            digest=str(attachment.get("sha256", ""))[:12],
                        )
                    )
                message = "\n".join(lines)
        elif parts == ["clear"]:
            self._pending_image_attachments.clear()
            message = tr("attachments.cleared", self._locale)
        elif len(parts) == 2 and parts[0] == "remove" and parts[1].isdigit():
            index = int(parts[1])
            if 1 <= index <= len(self._pending_image_attachments):
                self._pending_image_attachments.pop(index - 1)
                message = tr("attachments.removed", self._locale, index=index)
            else:
                message = tr("attachments.usage", self._locale)
        else:
            message = tr("attachments.usage", self._locale)
        self._refresh_attachment_strip()
        self._append(Static(escape(message), classes="log-line"))

    # 把 @文件 解析为有界工作区引用，只注入路径和读取规则而不盲目附加全文
    def _augment_file_references(self, content: str, visible_content: str) -> str:
        raw_refs = [
            token[1:].strip(".,;，。；:：")
            for token in visible_content.split()
            if token.startswith("@") and len(token) > 1
        ][:8]
        resolved: list[str] = []
        for raw in raw_refs:
            candidate = (self._workspace / raw).resolve()
            try:
                candidate.relative_to(self._workspace)
            except ValueError:
                continue
            if candidate.is_file():
                resolved.append(candidate.relative_to(self._workspace).as_posix())
                continue
            matches = [
                path
                for path in self._workspace.rglob(f"*{Path(raw).name}*")
                if path.is_file()
                and ".git" not in path.parts
                and ".coderook" not in path.parts
            ][:2]
            if len(matches) == 1:
                resolved.append(matches[0].relative_to(self._workspace).as_posix())
        if not resolved:
            return content
        unique = list(dict.fromkeys(resolved))
        return (
            content
            + "\n\nBounded file references selected by the user: "
            + json.dumps(unique, ensure_ascii=False)
            + ". Read only the ranges needed for this task; do not inject entire files by default."
        )

    # 将用户可见的 Shell 与文件引用语法转换为模型执行提示，同时保留原始展示文本
    def _prepare_model_content(self, visible_content: str) -> str:
        content = visible_content
        if visible_content.startswith("!") and len(visible_content) > 1:
            command = visible_content[1:].strip()
            content = (
                "The user explicitly requested this exact shell command. Run it through the "
                "normal permission and sandbox tool pipeline, then report its exit status and "
                f"important output without changing the command: {command}"
            )
        return self._augment_file_references(content, visible_content)

    # 在当前会话恢复等待输入后发送一条排队消息并刷新队列状态
    def _flush_queued_message(self) -> None:
        if self._busy or not self._queued_messages:
            return
        prompt = self._prompt()
        if prompt is None or prompt.disabled:
            return
        visible_content = self._queued_messages.popleft()
        content = self._prepare_model_content(visible_content)
        self._update_status_bar()
        self._begin_message(
            prompt,
            content,
            self._input_runtime_mode,
            visible_content=visible_content,
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
            tr("shell.planning", self._locale)
            if runtime_mode == RuntimeMode.PLAN
            else tr("shell.running", self._locale)
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
        self._refresh_attachment_strip()
        self.run_worker(
            self._do_send_message(
                content,
                runtime_mode,
                attachments=attachments,
                display_content=shown,
            ),
            name="send_message",
            exclusive=False,
        )

    # 进入 Goal 创建或恢复运行态，并在 worker 中执行长生命周期 IPC
    def _begin_goal_command(
        self,
        prompt: ChatTextArea,
        action: str,
        objective: str,
        *,
        command_params: dict[str, object] | None = None,
        draft: str = "",
    ) -> None:
        self._busy = True
        self._cancel_armed = False
        prompt.text = ""
        prompt.disabled = False
        prompt.read_only = False
        prompt.border_title = tr("shell.running", self._locale)
        visible = (
            objective
            if action == "create"
            else tr("app.goal.resume_label", self._locale)
        )
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
            self._do_goal_command(
                action,
                objective,
                command_params=command_params,
                draft=draft,
            ),
            name=f"goal_{action}",
            exclusive=False,
        )

    # 发送 Goal IPC 命令、更新顶栏状态并渲染可审计摘要
    async def _do_goal_command(
        self,
        action: str,
        value: str = "",
        *,
        command_params: dict[str, object] | None = None,
        draft: str = "",
    ) -> None:
        retry_draft = draft or (
            f"/goal {value}" if action == "create" else f"/goal {action}"
        )
        if self._client is None or self._session_id is None:
            if action in {"create", "resume"}:
                self._busy = False
                self._restore_unsent_draft(
                    retry_draft,
                    tr("app.goal.draft_reconnected", self._locale),
                )
                self._update_header("disconnected")
            return
        method = f"goal.{action}"
        params: dict[str, object] = {"session_id": self._session_id}
        if action == "create":
            if command_params is None:
                params["objective"] = value
            else:
                params.update(command_params)
                params["session_id"] = self._session_id
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
                lines = [self._format_goal_summary(item, detailed=False) for item in goals]
                self._append(
                    Static(
                        f"[bold cyan]{tr('app.goals.title', self._locale)}[/bold cyan]\n"
                        + (
                            "\n".join(lines)
                            if lines
                            else f"[dim]{tr('app.goals.empty', self._locale)}[/dim]"
                        ),
                        classes="log-line",
                    )
                )
            else:
                raw_goal = result.get("goal")
                if isinstance(raw_goal, dict):
                    self._apply_goal_state(raw_goal)
                    self._append(
                        Static(
                            f"[bold cyan]{tr('app.goal.title', self._locale)}[/bold cyan]  "
                            + self._format_goal_summary(raw_goal),
                            classes="log-line",
                        )
                    )
                else:
                    self._active_goal_id = None
                    self._active_goal_status = ""
                    self._append(
                        Static(
                            f"[dim]{tr('app.goal.empty', self._locale)}[/dim]",
                            classes="log-line",
                        )
                    )
                    self._update_header("running" if self._busy else "ready")
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("goal", exc, action="submission")
            if action in {"create", "resume"}:
                self._busy = False
                self._update_header("ready")
                self._restore_unsent_draft(
                    retry_draft,
                    tr("app.goal.draft_failed", self._locale),
                )

    # 把 Goal 字典同步为 TUI 顶栏所需的最小状态
    def _apply_goal_state(self, goal: dict[str, Any]) -> None:
        status = str(goal.get("status", ""))
        self._active_goal_id = str(goal.get("id", "")) or None
        self._active_goal_status = status
        if status in {"completed", "cleared"}:
            self._active_goal_id = None
        self._update_header("running" if self._busy else "ready")

    # 生成 Goal 状态与证据摘要，并为列表模式折叠明细
    def _format_goal_summary(self, goal: object, *, detailed: bool = True) -> str:
        if not isinstance(goal, dict):
            return f"[red]{tr('app.goal.invalid_payload', self._locale)}[/red]"
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
        token_limit = str(budget) if budget is not None else "unbounded"
        elapsed_seconds = max(0, int(goal.get("elapsed_ms", 0) or 0)) // 1000
        max_wall_seconds = max(1, int(goal.get("max_wall_seconds", 1800) or 1800))
        linked_runs = goal.get("linked_run_ids", [])
        runs = len(linked_runs) if isinstance(linked_runs, list) else 0
        auto_turns_used = max(0, int(goal.get("auto_turns_used", 0) or 0))
        max_auto_turns = max(1, int(goal.get("max_auto_turns", 3) or 3))
        raw_criteria_value = goal.get("completion_criteria", [])
        raw_criteria = raw_criteria_value if isinstance(raw_criteria_value, list) else []
        declared = [
            str(item)
            for item in raw_criteria
            if isinstance(item, str) and item.strip()
        ]
        raw_evidence = goal.get("completion_evidence", [])
        evidence_items = raw_evidence if isinstance(raw_evidence, list) else []
        covered: set[str] = set()
        for item in evidence_items:
            if not isinstance(item, dict):
                continue
            raw_covered = item.get("covered_criteria", [])
            if isinstance(raw_covered, list):
                covered.update(
                    str(criterion)
                    for criterion in raw_covered
                    if isinstance(criterion, str) and criterion.strip()
                )
        pending = [criterion for criterion in declared if criterion not in covered]
        current_run = str(goal.get("current_run_id") or "")
        running = f"  run={escape(current_run[:12])}" if current_run else ""
        reason = str(goal.get("status_reason") or "").strip()
        paused_reason = str(goal.get("paused_reason") or "").strip()
        confirmation = bool(goal.get("paused_needs_confirmation", False))
        summary = (
            f"[bold]{goal_id}[/bold]  [{status_color}]{status}[/{status_color}]  {objective}"
            f"\n[dim]round={runs}  auto={auto_turns_used}/{max_auto_turns}  "
            f"tokens={tokens_used}/{token_limit}  elapsed={elapsed_seconds}s/"
            f"{max_wall_seconds}s  evidence={len(evidence_items)}/"
            f"{len(declared)}  paused_needs_confirmation="
            f"{'yes' if confirmation else 'no'}{running}[/dim]"
        )
        if not detailed:
            return summary
        details: list[str] = [summary]
        if pending:
            details.append(
                f"[yellow]{tr('app.goal.incomplete', self._locale)}[/yellow]  "
                + escape(" · ".join(pending[:5]))
                + (f" · +{len(pending) - 5}" if len(pending) > 5 else "")
            )
        elif declared:
            details.append(
                f"[green]{tr('app.goal.all_evidenced', self._locale)}[/green]"
            )
        else:
            details.append(
                f"[yellow]{tr('app.goal.no_criteria', self._locale)}[/yellow]"
            )
        evidence_lines: list[str] = []
        for item in evidence_items[-3:]:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "evidence"))
            description = str(item.get("summary") or item.get("reference") or "")
            evidence_lines.append(f"{escape(kind)}: {escape(description)}")
        if evidence_lines:
            details.append(
                f"[cyan]{tr('app.goal.evidence', self._locale)}[/cyan]  "
                + " · ".join(evidence_lines)
            )
        if paused_reason or reason:
            details.append(
                f"[yellow]{tr('app.goal.pause_reason', self._locale)}[/yellow]  "
                f"{escape(paused_reason or 'unspecified')}"
                + (f" · {escape(reason)}" if reason else "")
            )
        if confirmation:
            details.append(
                f"[bold yellow]{tr('app.goal.resume_confirm', self._locale)}[/bold yellow]"
            )
        return "\n".join(details)

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
            self._append(
                Static(
                    f"[bold cyan]{tr('app.compaction.title', self._locale)}[/bold cyan]  "
                    "[dim]"
                    + tr(
                        "app.compaction.summary",
                        self._locale,
                        summary=summary_tokens,
                        messages=retained_messages,
                        tokens=retained_tokens,
                        saved=saved_tokens,
                        quality=f"{quality:.0%}",
                    )
                    + "[/dim]",
                    classes="log-line",
                )
            )
            if summary_path:
                summary_location = tr(
                    "app.compaction.file",
                    self._locale,
                    path=summary_path,
                )
                self._append(
                    Static(
                        f"[dim]  {summary_location}[/dim]",
                        classes="log-line",
                    )
                )
        except IpcActionError as exc:
            self._show_safe_error("compaction", exc)

    def _restore_ready_prompt(self) -> None:
        prompt = self._prompt()
        if prompt is not None:
            prompt.disabled = False
            prompt.read_only = False
            if self._input_runtime_mode == RuntimeMode.PLAN:
                prompt.border_title = tr("shell.plan", self._locale)
            else:
                prompt.border_title = tr("shell.connected", self._locale)
            prompt.focus()

    # 恢复未被 Core 确认接收的草稿；已有新输入时不覆盖，断线期间保持禁用
    def _restore_unsent_draft(self, draft: str, border_title: str) -> bool:
        prompt = self._prompt()
        if prompt is None:
            return False
        restored = False
        if draft and not prompt.text.strip():
            prompt.text = draft
            restored = True
        prompt.disabled = self._client is None
        prompt.read_only = False
        prompt.border_title = border_title
        if not prompt.disabled:
            prompt.focus()
        return restored

    # 将 Core 返回的四维 authority 快照映射到 TUI 状态
    def _apply_authority_snapshot(self, snapshot: dict[str, Any]) -> None:
        mode = RuntimeMode(str(snapshot.get("mode", RuntimeMode.ACT.value)))
        profile = AuthorityProfile(str(snapshot.get("profile", AuthorityProfile.ASK.value)))
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
        result = await ipc_actions.get_authority_snapshot(self._client, self._session_id)
        self._apply_authority_snapshot(dict(result.get("snapshot", {})))

    # 持久化并应用用户选择的权限姿态，不改变工作模式
    async def _select_authority_preset(
        self,
        preset: str,
        *,
        announce: bool = True,
    ) -> None:
        selected = next(
            (item for item in permission_presets(self._locale) if item[0] == preset),
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
            self._show_safe_error("authority", exc)
            self._restore_ready_prompt()
            return
        self._restore_ready_prompt()
        self._update_header("plan" if self._input_runtime_mode == RuntimeMode.PLAN else "ready")
        if announce:
            changed = tr(
                "app.permission.changed",
                self._locale,
                label=escape(label),
            )
            self._append(
                Static(
                    f"[bold cyan]{changed}[/bold cyan]",
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
            self._show_safe_error("authority", exc)
            self._restore_ready_prompt()
            return
        self._restore_ready_prompt()
        self._update_header("plan" if mode == RuntimeMode.PLAN else "ready")
        if announce:
            changed = tr(
                "app.mode.changed",
                self._locale,
                mode=escape(mode.value),
            )
            self._append(
                Static(
                    f"[bold cyan]{changed}[/bold cyan]",
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
            self._show_safe_error("authority", exc)
            return
        self._update_header("plan" if self._input_runtime_mode == RuntimeMode.PLAN else "ready")
        self._show_trust_status()

    # 在 transcript 中显示当前工作区信任状态
    def _show_trust_status(self) -> None:
        changed = tr(
            "app.trust.changed",
            self._locale,
            trust=self._workspace_trust.value,
        )
        self._append(
            Static(
                f"[bold cyan]{changed}[/bold cyan]",
                classes="log-line",
            )
        )

    # 在 transcript 中如实显示操作系统隔离能力
    def _show_sandbox_status(self) -> None:
        available = bool(self._sandbox.get("available", False))
        kind_value = str(self._sandbox.get("kind", "none"))
        kind = escape(kind_value)
        reason = escape(str(self._sandbox.get("reason", "unavailable")))
        partial = available and kind_value == "windows_acl"
        state = "PARTIAL" if partial else "ENFORCED" if available else "DEGRADED"
        color = "yellow" if partial or not available else "green"
        explanation = tr(
            "app.sandbox.partial"
            if partial
            else "app.sandbox.available"
            if available
            else "app.sandbox.unavailable",
            self._locale,
        )
        self._append(
            Static(
                f"[bold cyan]{tr('app.sandbox.title', self._locale)}[/bold cyan]  "
                f"[{color}]{state}[/{color}]"
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
                body = f"[dim]{tr('app.tasks.empty', self._locale)}[/dim]"
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
                lines = [
                    f"[bold cyan]{tr('app.tasks.title', self._locale)}[/bold cyan]"
                ]
                for task in tasks:
                    status = str(task.get("status", "pending"))
                    subject = escape(str(task.get("subject", "")))
                    task_id = task.get("id", "")
                    blocked = task.get("blocked_by", [])
                    blocked_text = ""
                    if blocked:
                        blocked_label = tr(
                            "app.tasks.blocked",
                            self._locale,
                            items=escape(str(blocked)),
                        )
                        blocked_text = f"  [dim]{blocked_label}[/dim]"
                    lines.append(f"{markers.get(status, '[?]')} #{task_id} {subject}{blocked_text}")
                body = "\n".join(lines)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("tasks", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 通过 daemon-owned launcher 启动持久 Worker 并展示冻结 route/worktree
    async def _do_worker_start(
        self,
        *,
        description: str,
        prompt: str,
        profile: str,
        route_id: str,
        model: str,
        backend: str = "builtin",
        read_only: bool,
        exact_files: list[str],
        write_roots: list[str],
        token_budget: int | None,
    ) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.start_worker(
                self._client,
                self._session_id,
                description=description,
                prompt=prompt,
                profile=profile,
                route_id=route_id,
                model=model,
                backend=backend,
                read_only=read_only,
                exact_files=exact_files,
                write_roots=write_roots,
                token_budget=token_budget,
            )
            worker_id = escape(str(result.get("worker_id", "")))
            route = escape(str(result.get("route_id", "")))
            model = escape(str(result.get("model", "")))
            worktree = escape(str(result.get("worktree", "")))
            backend_name = escape(str(result.get("backend", backend)))
            enforcement = escape(
                str(result.get("sandbox_enforcement", "unavailable"))
            )
            scope = "read-only" if bool(result.get("read_only", True)) else worktree
            started = tr(
                "app.worker.started",
                self._locale,
                worker=worker_id,
            )
            self._append(
                Static(
                    f"[green]{started}[/green]  "
                    f"[dim]{backend_name} · {route}/{model} · {scope} · {enforcement}[/dim]",
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._show_safe_error("worker-start", exc)
        finally:
            self._restore_ready_prompt()

    # 查询并展示严格绑定当前 session 的单个 Worker 状态
    async def _show_worker_status(self, worker_id: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.get_worker_status(
                self._client,
                self._session_id,
                worker_id,
            )
            worker = dict(result.get("worker", {}))
            status = escape(str(worker.get("status", "unknown")))
            description = escape(str(worker.get("description", "")))
            route = escape(str(worker.get("route", "")))
            model = escape(str(worker.get("model", "")))
            attempt = escape(str(worker.get("attempt", "")))
            maximum = escape(str(worker.get("max_attempts", "")))
            worktree = escape(str(worker.get("worktree", ""))) or "read-only"
            summary = escape(str(worker.get("summary", "")))
            body = (
                f"[bold cyan]Worker {escape(worker_id)}[/bold cyan]\n"
                f"{description}\n"
                f"[dim]{status} · attempt {attempt}/{maximum} · {route}/{model} · "
                f"{worktree}[/dim]"
            )
            if summary:
                body += f"\n{summary}"
            self._append(Static(body, classes="log-line"))
        except (IpcActionError, TypeError, ValueError) as exc:
            self._show_safe_error("worker-status", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 按原 WorkerRecord 的冻结边界启动新的 attempt
    async def _do_worker_retry(self, worker_id: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.retry_worker(
                self._client,
                self._session_id,
                worker_id,
            )
            attempt = escape(str(result.get("attempt", "")))
            status = escape(str(result.get("status", "")))
            retried = tr(
                "app.worker.retried",
                self._locale,
                worker=escape(worker_id),
            )
            self._append(
                Static(
                    f"[green]{retried}[/green]  "
                    f"[dim]attempt {attempt} · {status}[/dim]",
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._show_safe_error("worker-retry", exc)
        finally:
            self._restore_ready_prompt()

    # 加载并展示当前会话的持久 Worker 状态、attempt、预算和简短结果
    async def _show_workers(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.get_workers(
                self._client,
                session_id=self._session_id,
            )
            workers = list(result.get("workers", []))
            if not workers:
                body = f"[dim]{tr('app.worker.empty', self._locale)}[/dim]"
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
                    worktree = str(worker.get("worktree", "")).strip()
                    handoff = str(worker.get("handoff_status", "read_only"))
                    verification = str(worker.get("verification_status", "not_reported"))
                    if worktree:
                        lines.append(
                            "    "
                            f"[dim]worktree {escape(worktree)} · handoff "
                            f"{escape(handoff)} · verification {escape(verification)}[/dim]"
                        )
                    diff_stat = str(worker.get("diff_stat", "")).strip()
                    if diff_stat:
                        lines.append(f"    [dim]{escape(_preview(diff_stat, 160))}[/dim]")
                    summary = str(worker.get("summary", "")).strip()
                    if summary:
                        lines.append(f"    [dim]{escape(_preview(summary, 140))}[/dim]")
                body = "\n".join(lines)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("workers", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 读取并展示指定 Worker 的有界持久进度事件
    async def _show_worker_events(self, worker_id: str, after_cursor: int = 0) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.get_worker_events(
                self._client,
                self._session_id,
                worker_id,
                after_cursor=after_cursor,
            )
            events = list(result.get("events", []))
            event_title = tr(
                "app.worker.events",
                self._locale,
                worker=escape(worker_id),
            )
            lines = [f"[bold cyan]{event_title}[/bold cyan]"]
            for event in events:
                cursor = escape(str(event.get("cursor", "")))
                kind = escape(str(event.get("kind", "event")))
                summary = escape(str(event.get("summary", "")))
                lines.append(f"  [dim]#{cursor} {kind}[/dim]  {summary}")
            if not events:
                lines.append(f"[dim]{tr('app.worker.no_events', self._locale)}[/dim]")
            self._append(Static("\n".join(lines), classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("worker-events", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 向运行中的 Worker 发送 followup 并显示新事件游标
    async def _do_worker_followup(self, worker_id: str, message: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.followup_worker(
                self._client,
                self._session_id,
                worker_id,
                message,
            )
            sent = tr(
                "app.worker.followup",
                self._locale,
                worker=escape(worker_id),
            )
            self._append(
                Static(
                    f"[green]{sent}[/green]  "
                    f"[dim]cursor {escape(str(result.get('event_cursor', '')))}[/dim]",
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._show_safe_error("worker-followup", exc)
        finally:
            self._restore_ready_prompt()

    # 先展示完整 Worker 补丁，再携权威摘要记录人工审查结果
    async def _do_worker_review(
        self,
        worker_id: str,
        approved: bool,
        *,
        confirmed: bool = False,
        expected_digest: str = "",
    ) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.review_worker(
                self._client,
                self._session_id,
                worker_id,
                approved=approved,
                confirmed=confirmed,
                expected_digest=expected_digest,
            )
            status = escape(str(result.get("handoff_status", "")))
            digest = str(result.get("state_digest", ""))
            if approved and (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("worker review did not return a valid state digest")
            if bool(result.get("preview_only", False)):
                changed_files = [str(item) for item in result.get("changed_files", [])]
                diff = str(result.get("diff", ""))
                if bool(result.get("diff_truncated", False)) or not diff:
                    raise ValueError("worker review preview is incomplete")
                files_text = ", ".join(escape(item) for item in changed_files)
                detail = (
                    f"[bold]{tr('app.worker.patch_title', self._locale)}[/bold]\n"
                    f"[dim]files: {files_text or '(none)'}[/dim]\n"
                    f"[cyan]{escape(diff)}[/cyan]\n"
                    f"[bold]digest[/bold] [cyan]{escape(digest)}[/cyan]\n"
                    "[yellow]"
                    + tr(
                        "app.worker.review_confirm",
                        self._locale,
                        command=(
                            f"/workers review {escape(worker_id)} approve "
                            f"{escape(digest)} --yes"
                        ),
                    )
                    + "[/yellow]"
                )
            elif approved:
                pending = tr(
                    "app.worker.review_pending",
                    self._locale,
                    status=status,
                )
                apply_command = (
                    f"/workers apply {escape(worker_id)} {escape(digest)} --yes"
                )
                detail = (
                    f"[dim]{pending}[/dim]\n"
                    f"[bold]digest[/bold] [cyan]{escape(digest)}[/cyan]\n"
                    f"[dim]{tr('app.worker.review_confirm', self._locale, command=apply_command)}"
                    "[/dim]"
                )
            else:
                rejected = tr(
                    "app.worker.review_rejected",
                    self._locale,
                    status=status,
                )
                detail = f"[dim]{rejected}[/dim]"
            review_title = tr(
                "app.worker.review_title",
                self._locale,
                worker=escape(worker_id),
            )
            self._append(
                Static(
                    f"[green]{review_title}[/green]  {detail}",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("worker-review", exc)
        finally:
            self._restore_ready_prompt()

    # 应用与人工审查摘要完全匹配的 Worker handoff，并明确不创建提交或推送
    async def _do_worker_apply(self, worker_id: str, expected_digest: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.apply_worker(
                self._client,
                self._session_id,
                worker_id,
                expected_digest,
            )
            if result.get("handoff_status") != "applied":
                raise ValueError("worker apply did not return an applied handoff")
            applied = tr(
                "app.worker.applied",
                self._locale,
                worker=escape(worker_id),
            )
            self._append(
                Static(
                    f"[green]{applied}[/green]  "
                    f"[dim]{tr('app.worker.not_committed', self._locale)}[/dim]",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("worker-apply", exc)
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
                    (item for item in servers if str(item.get("name", "")) == server_name),
                    None,
                )
                body = (
                    render_mcp_tools(target, locale=self._locale)
                    if target is not None
                    else "[dim]"
                    + tr(
                        "app.mcp.not_found",
                        self._locale,
                        name=escape(server_name),
                    )
                    + "[/dim]"
                )
            else:
                body = render_mcp_servers(servers, locale=self._locale)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("mcp", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 加载并展示引用状态、大小和 GC 候选 Artifact 清单
    async def _show_artifacts(self, days: int = 30) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_artifacts(self._client, days=days)
            self._append(
                Static(
                    render_artifacts(result, locale=self._locale),
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._show_safe_error("artifacts", exc, action="inspection")
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
            self._append(
                Static(
                    render_artifact_gc(result, locale=self._locale),
                    classes="log-line",
                )
            )
        except IpcActionError as exc:
            self._show_safe_error("artifact-gc", exc)
        finally:
            self._restore_ready_prompt()

    # 加载并展示 hook 配置表与最近执行记录
    async def _show_hooks(self) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_hooks(self._client)
            body = render_hooks(result, locale=self._locale)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("hooks", exc, action="inspection")
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
            self._show_safe_error("hook-rerun", exc)
        finally:
            self._restore_ready_prompt()

    # 加载并展示当前项目记忆条目
    async def _show_memory(self) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.list_memories(self._client)
            memories = list(result.get("memories", []))
            settings = result.get("settings", {})
            body = render_memory(
                memories,
                settings if isinstance(settings, dict) else {},
                locale=self._locale,
            )
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("memory", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 执行新增、编辑、固定、过期或自动保存设置并回显权威结果
    async def _do_memory_action(
        self,
        action: str,
        params: dict[str, object],
    ) -> None:
        if self._client is None:
            return
        try:
            if action == "add":
                result = await ipc_actions.add_memory(
                    self._client,
                    name=str(params["name"]),
                    body=str(params["body"]),
                    source_session_id=self._session_id or "",
                )
            elif action == "edit":
                result = await ipc_actions.edit_memory(
                    self._client,
                    str(params["memory_id"]),
                    body=params["body"],
                )
            elif action == "pin":
                result = await ipc_actions.pin_memory(
                    self._client,
                    str(params["memory_id"]),
                    pinned=bool(params["pinned"]),
                )
            elif action == "expire":
                expires_at = params.get("expires_at")
                result = await ipc_actions.expire_memory(
                    self._client,
                    str(params["memory_id"]),
                    expires_at=str(expires_at) if expires_at is not None else None,
                )
            elif action == "auto":
                result = await ipc_actions.set_memory_auto_save(
                    self._client,
                    str(params["auto_save"]),
                )
            else:
                raise ValueError(f"unknown memory action: {action}")
            if action == "auto":
                settings = result.get("settings", {})
                mode = settings.get("auto_save", "") if isinstance(settings, dict) else ""
                message = tr("app.memory.auto", self._locale, mode=mode)
            else:
                memory = result.get("memory", {})
                memory_id = memory.get("id", "") if isinstance(memory, dict) else ""
                message = tr(
                    "app.memory.action",
                    self._locale,
                    action=action,
                    id=memory_id,
                )
            self._append(Static(f"[green]{escape(message)}[/green]", classes="log-line"))
        except (IpcActionError, KeyError, ValueError) as exc:
            self._show_safe_error(f"memory-{action}", exc)
        finally:
            self._restore_ready_prompt()

    # 删除指定项目记忆并在滚动区回显结果
    async def _do_memory_delete(self, memory_id: str) -> None:
        if self._client is None:
            return
        try:
            result = await ipc_actions.delete_memory(self._client, memory_id)
            if result.get("deleted"):
                message = tr(
                    "app.memory.deleted",
                    self._locale,
                    id=escape(memory_id),
                )
                self._append(
                    Static(
                        f"[green]{message}[/green]",
                        classes="log-line",
                    )
                )
            else:
                message = tr(
                    "app.memory.missing",
                    self._locale,
                    id=escape(memory_id),
                )
                self._append(
                    Static(
                        f"[yellow]{message}[/yellow]",
                        classes="log-line",
                    )
                )
        except IpcActionError as exc:
            self._show_safe_error("memory-delete", exc)
        finally:
            self._restore_ready_prompt()

    # 加载并展示后台任务中心：bg 任务列表 + 子代理汇总，或单个任务全量输出
    async def _show_jobs(self, show_id: str = "") -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await ipc_actions.get_background(
                self._client,
                self._session_id,
                job_id=show_id,
            )
            jobs = list(result.get("jobs", []))
            if show_id:
                body = render_job_output(jobs)
            else:
                workers = await ipc_actions.get_workers(
                    self._client,
                    self._session_id,
                )
                worker_records = list(workers.get("workers", []))
                body = render_jobs(jobs) + "\n\n" + render_workers_summary(worker_records)
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("jobs", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 取消后台任务（bg-* 或 Worker/子代理）并在滚动区回显结果
    async def _do_job_cancel(self, job_id: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            if job_id.startswith("bg-"):
                await ipc_actions.cancel_background(
                    self._client,
                    self._session_id,
                    job_id,
                )
            else:
                result = await ipc_actions.cancel_worker(
                    self._client,
                    self._session_id,
                    job_id,
                )
                status = str(result.get("status", ""))
                self._append(
                    Static(
                        f"[green]cancelled {escape(job_id)}[/green]  [dim]{escape(status)}[/dim]",
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
            self._show_safe_error("job-cancel", exc)
        finally:
            self._restore_ready_prompt()

    # 从 Core durable ledger 加载 workflow 列表或单个 Work Graph projection
    async def _show_workflow(self, workflow_id: str = "") -> None:
        if self._client is None:
            return
        try:
            if workflow_id:
                result = await ipc_actions.get_workflow(self._client, workflow_id)
                body = render_workflow_graph(dict(result.get("workflow", {})))
            else:
                result = await ipc_actions.list_workflows(self._client)
                body = render_workflow_list(list(result.get("workflows", [])))
            self._append(Static(body, classes="log-line"))
        except IpcActionError as exc:
            self._show_safe_error("workflow", exc, action="inspection")
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
                    f"[green]{tr('app.workflow.started', self._locale)}[/green] "
                    f"{workflow_id}",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError) as exc:
            self._show_safe_error("workflow-start", exc)
        finally:
            self._restore_ready_prompt()

    # 读取当前 session 最近一次 durable Turn Receipt，缺失时保留纯 Diff 审查
    async def _latest_change_receipt(self) -> dict[str, Any] | None:
        if self._client is None or self._session_id is None:
            return None
        try:
            context = await ipc_actions.get_context(self._client, self._session_id)
            last_run_id = str(context.get("last_run_id") or "")
            if last_run_id:
                return dict(await ipc_actions.inspect_turn(self._client, last_run_id))
        except (IpcActionError, ValueError, TypeError):
            log.warning("recent receipt unavailable for Change Center", exc_info=True)
        return None

    # 把给定权威 Diff 展示为独立面板或可聚焦 overlay，并返回是否接管了输入焦点
    def _present_change_center(
        self,
        diff_result: dict[str, Any],
        receipt_result: dict[str, Any] | None,
    ) -> bool:
        if not self.is_running:
            panel = ChangeCenterPanel()
            panel.update(diff_result, receipt_result)
            self._append(
                Static(
                    panel.render(width=100, height=30, locale=self._locale),
                    classes="log-line",
                )
            )
            return False
        try:
            self.query_one(ChangeCenterOverlay).remove()
        except Exception:
            pass
        self.mount(
            ChangeCenterOverlay(
                diff_result,
                receipt_result,
                locale=self._locale,
            )
        )
        return True

    # 加载工作区 diff 与最近 durable receipt 并打开可聚焦 Change Center
    async def _show_changes(self) -> None:
        if self._client is None:
            return
        opened = False
        self._change_state_digest = ""
        self._change_review_scope = ""
        try:
            diff_result = await ipc_actions.get_diff(self._client)
            payload = dict(diff_result.get("payload", {}))
            if "error" in payload:
                error = dict(payload["error"])
                raise ValueError(str(error.get("message", "workspace diff unavailable")))
            state_digest = str(payload.get("state_digest", ""))
            scope = str(payload.get("scope", ""))
            if len(state_digest) == 64 and scope == "all":
                self._change_state_digest = state_digest
                self._change_review_scope = scope
            receipt_result = await self._latest_change_receipt()
            opened = self._present_change_center(diff_result, receipt_result)
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("inspection", exc, action="inspection")
        finally:
            if not opened:
                self._restore_ready_prompt()

    # 保留 /diff 兼容别名并统一打开 Change Center overlay
    async def _show_diff(self) -> None:
        await self._show_changes()

    # Esc 关闭 Change Center 后恢复 composer 焦点与当前模式标题
    def on_change_center_overlay_dismissed(
        self,
        message: ChangeCenterOverlay.Dismissed,
    ) -> None:
        message.overlay.remove()
        self._restore_ready_prompt()

    # 请求 Core stage 用户已确认的文件并展示 stage 后权威统计
    async def _stage_changes(self, paths: list[str]) -> None:
        if self._client is None or self._session_id is None:
            return
        opened = False
        try:
            if not self._change_state_digest or self._change_review_scope != "all":
                raise ValueError("run /diff before confirming stage")
            result = await ipc_actions.stage_changes(
                self._client,
                self._session_id,
                paths,
                self._change_state_digest,
            )
            payload = dict(result.get("payload", {}))
            state_digest = str(payload.get("state_digest", ""))
            scope = str(payload.get("scope", ""))
            if len(state_digest) != 64 or scope != "staged":
                self._change_state_digest = ""
                self._change_review_scope = ""
                raise ValueError("stage did not return a complete staged review")
            self._change_state_digest = state_digest
            self._change_review_scope = scope
            files = [
                str(item.get("path", ""))
                for item in payload.get("files", [])
                if isinstance(item, dict)
            ]
            self._append(
                Static(
                    "[bold green]"
                    + tr(
                        "app.changes.staged",
                        self._locale,
                        count=len(files),
                        files=escape(", ".join(files)),
                    )
                    + "[/bold green]",
                    classes="log-line",
                )
            )
            receipt_result = await self._latest_change_receipt()
            opened = self._present_change_center(result, receipt_result)
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("change-center", exc)
        finally:
            if not opened:
                self._restore_ready_prompt()

    # 请求 Core 创建本地 commit 并展示 hash、文件数和无 push 承诺
    async def _commit_changes(self, message: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            if not self._change_state_digest or self._change_review_scope != "staged":
                self._append(
                    Static(
                        "[yellow]"
                        + escape(
                            tr("app.changes.review_stage_first", self._locale)
                        )
                        + "[/yellow]",
                        classes="log-line",
                    )
                )
                return
            result = await ipc_actions.commit_changes(
                self._client,
                self._session_id,
                message,
                self._change_state_digest,
            )
            self._change_state_digest = ""
            self._change_review_scope = ""
            commit = escape(str(result.get("commit", ""))[:12])
            subject = escape(str(result.get("subject", "")))
            files = list(result.get("files", []))
            committed = tr(
                "app.changes.committed",
                self._locale,
                commit=commit,
                subject=subject,
            )
            self._append(
                Static(
                    f"[bold green]{committed}[/bold green]\n"
                    f"[dim]{tr('app.changes.commit_meta', self._locale, count=len(files))}[/dim]",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("change-center", exc)
        finally:
            self._restore_ready_prompt()

    # 加载最近一次 run 的可恢复 checkpoint 并打开选择器
    async def _show_rewind_picker(self) -> None:
        if self._client is None or self._session_id is None:
            return
        self._pending_rewind = None
        try:
            checkpoints = [
                dict(item)
                for item in await ipc_actions.list_checkpoints(self._client, self._session_id)
                if item.get("status") == "ready"
            ]
            if not checkpoints:
                self._append(
                    Static(
                        f"[dim]{tr('app.rewind.empty', self._locale)}[/dim]",
                        classes="log-line",
                    )
                )
                self._restore_ready_prompt()
                return
            self.mount(
                CheckpointPicker(checkpoints, locale=self._locale),
                before="#prompt",
            )
        except IpcActionError as exc:
            self._show_safe_error("rewind", exc)
            self._restore_ready_prompt()

    # 关闭 checkpoint 选择器且不修改文件
    async def on_checkpoint_picker_dismissed(
        self,
        message: CheckpointPicker.Dismissed,
    ) -> None:
        message.picker.remove()
        self._restore_ready_prompt()

    # 只预览用户选择的 checkpoint，保存摘要但不在选择动作中修改文件
    async def on_checkpoint_picker_selected(
        self,
        message: CheckpointPicker.Selected,
    ) -> None:
        message.picker.remove()
        if self._client is None or self._session_id is None:
            self._restore_ready_prompt()
            return
        try:
            result = await ipc_actions.preview_rewind(
                self._client,
                self._session_id,
                message.checkpoint_id,
            )
            paths = [str(path) for path in result.get("paths", [])]
            restorable = [str(path) for path in result.get("restorable", [])]
            unchanged = [str(path) for path in result.get("already_restored", [])]
            conflicts = [str(path) for path in result.get("conflicts", [])]
            digest = str(result.get("state_digest", ""))
            if len(digest) != 64:
                raise ValueError("rewind preview did not return a valid state digest")
            if conflicts:
                self._pending_rewind = None
            else:
                self._pending_rewind = {
                    "session_id": self._session_id,
                    "checkpoint_id": message.checkpoint_id,
                    "state_digest": digest,
                }
            confirmation = (
                f"[red]{tr('app.rewind.conflicts', self._locale)}[/red]"
                if conflicts
                else f"[yellow]{tr('app.rewind.confirm', self._locale)}[/yellow]"
            )
            preview_title = tr(
                "app.rewind.preview",
                self._locale,
                checkpoint=escape(message.checkpoint_id),
            )
            self._append(
                Static(
                    f"[bold yellow]{preview_title}[/bold yellow]\n"
                    "[dim]"
                    + tr(
                        "app.rewind.preview_meta",
                        self._locale,
                        paths=escape(str(paths)),
                        restorable=escape(str(restorable)),
                        already=escape(str(unchanged)),
                        conflicts=escape(str(conflicts)),
                    )
                    + f"[/dim]\n{confirmation}",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError, TypeError) as exc:
            self._pending_rewind = None
            self._show_safe_error("rewind", exc)
        finally:
            self._restore_ready_prompt()

    # 使用仍匹配当前工作区的预览摘要执行一次显式确认恢复
    async def _confirm_rewind(self) -> None:
        pending = self._pending_rewind
        if self._client is None or self._session_id is None:
            return
        if pending is None or pending.get("session_id") != self._session_id:
            self._append(
                Static(
                    f"[yellow]{tr('app.rewind.no_pending', self._locale)}[/yellow]",
                    classes="log-line",
                )
            )
            self._restore_ready_prompt()
            return
        self._pending_rewind = None
        try:
            result = await ipc_actions.rewind(
                self._client,
                self._session_id,
                pending["checkpoint_id"],
                pending["state_digest"],
            )
            restored = [str(path) for path in result.get("restored", [])]
            unchanged = [str(path) for path in result.get("already_restored", [])]
            self._append(
                Static(
                    "[bold yellow]"
                    + tr(
                        "app.rewind.done",
                        self._locale,
                        checkpoint=escape(str(pending["checkpoint_id"])),
                        restored=escape(str(restored)),
                        already=escape(str(unchanged)),
                    )
                    + "[/bold yellow]",
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("rewind", exc)
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
            self._show_safe_error("context", exc, action="inspection")
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
                context = await ipc_actions.get_context(self._client, self._session_id)
                resolved_id = str(context.get("last_run_id") or "")
            if not resolved_id:
                raise ValueError("current session has no turn")
            result = await ipc_actions.inspect_turn(self._client, resolved_id)
            self._append(
                Static(
                    render_turn_inspector(dict(result), locale=self._locale),
                    classes="log-line",
                )
            )
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("inspection", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 在 Textual 消息泵中安排 durable 结果加载，同一 run 只渲染一次
    def _schedule_run_result(self, event: dict[str, Any]) -> bool:
        run_id = str(event.get("run_id", ""))
        session_id = self._session_id
        if (
            not run_id
            or not isinstance(session_id, str)
            or not session_id
            or run_id in self._rendered_result_runs
            or not self.is_running
        ):
            return False
        result_event = dict(event)
        result_event["_tui_session_id"] = session_id
        self._deferred_result_events.setdefault(session_id, {})[run_id] = result_event
        self._queue_run_result(result_event)
        return True

    # 把尚未完成的结果加载加入消息泵并使用独立 pending 集合抑制并发重复 worker
    def _queue_run_result(self, event: dict[str, Any]) -> None:
        run_id = str(event.get("run_id", ""))
        if (
            not run_id
            or run_id in self._rendered_result_runs
            or run_id in self._pending_result_runs
            or not self.is_running
        ):
            return
        self._pending_result_runs.add(run_id)
        self.call_later(self._start_run_result_worker, dict(event))

    # 会话重新激活后重试曾因切换或断线推迟的权威结果卡
    def _resume_deferred_run_results(self, session_id: str) -> None:
        for event in self._deferred_result_events.get(session_id, {}).values():
            self._queue_run_result(event)

    # 记录已成功挂载的结果并限制去重索引大小，避免长期开启 TUI 无界增长
    def _mark_result_rendered(self, run_id: str) -> None:
        if run_id in self._rendered_result_runs:
            return
        if len(self._rendered_result_order) >= 2048:
            expired = self._rendered_result_order.popleft()
            self._rendered_result_runs.discard(expired)
        self._rendered_result_order.append(run_id)
        self._rendered_result_runs.add(run_id)

    # 在下一轮消息泵创建结果协程，避免挂载阶段产生未消费 coroutine
    def _start_run_result_worker(self, event: dict[str, Any]) -> None:
        run_id = str(event.get("run_id", ""))
        try:
            self.run_worker(
                self._render_run_result(event),
                name=f"run_result:{run_id}",
                exclusive=False,
            )
        except Exception:
            self._pending_result_runs.discard(run_id)
            raise

    # 读取权威 turn receipt；短暂未投影时重试后回退到实时事件证据
    async def _render_run_result(self, event: dict[str, Any]) -> None:
        run_id = str(event.get("run_id", ""))
        target_session = event.get("_tui_session_id")
        try:
            if target_session != self._session_id:
                return
            inspection: dict[str, Any] | None = None
            failure: BaseException | None = None
            if self._client is not None:
                for delay_s in (0.0, 0.05, 0.1, 0.2, 0.4, 0.5, 0.75):
                    if target_session != self._session_id:
                        return
                    if delay_s:
                        await asyncio.sleep(delay_s)
                    if target_session != self._session_id:
                        return
                    try:
                        candidate = dict(
                            await ipc_actions.inspect_turn(self._client, run_id)
                        )
                        if target_session != self._session_id:
                            return
                        receipt = candidate.get("receipt", {})
                        if isinstance(receipt, dict) and receipt.get("finished_at"):
                            inspection = candidate
                            break
                    except (IpcActionError, ValueError, TypeError) as exc:
                        failure = exc
            if target_session != self._session_id:
                return
            result = self._run_evidence.finalize(event, inspection)
            self._append(RunResultCard(result, locale=self._locale))
            self._mark_result_rendered(run_id)
            session_events = self._deferred_result_events.get(str(target_session), {})
            session_events.pop(run_id, None)
            if not session_events:
                self._deferred_result_events.pop(str(target_session), None)
            if failure is not None and inspection is None:
                identifier = diagnostic_id("inspection", failure)
                log.error(
                    "result inspection failed diagnostic_id=%s run_id=%s detail=%s",
                    identifier,
                    run_id,
                    failure,
                )
        finally:
            self._pending_result_runs.discard(run_id)

    async def _show_session_picker(self) -> None:
        if self._client is None:
            return
        try:
            result = await self._client.send_command(
                "session.list",
                {"include_closed": True, "limit": 50},
            )
            sessions = [
                session for session in result.get("sessions", []) if session.get("mode") == "chat"
            ]
            try:
                self.query_one(SessionPicker).remove()
            except NoMatches:
                pass
            self.mount(
                SessionPicker(sessions, self._session_id, locale=self._locale),
                before="#prompt",
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("session", exc, action="session")
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
                    f"[yellow]{tr('app.provider.none', self._locale)}[/yellow]",
                    classes="log-line",
                )
            )
            return
        lines = [f"[bold cyan]{tr('app.provider.routes', self._locale)}[/bold cyan]"]
        for route in routes:
            marker = "[green]●[/green]" if active is not None and route.id == active.id else "○"
            meta = f"{route.wire_format} · {route.model}"
            if route.thinking != "off":
                meta += f" · thinking={route.thinking}"
            lines.append(f"{marker} [bold]{escape(route.id)}[/bold]  [dim]{escape(meta)}[/dim]")
        lines.append(f"[dim]{tr('app.provider.switch_hint', self._locale)}[/dim]")
        self._append(Static("\n".join(lines), classes="log-line"))

    # 返回活动或候选 route 的模型能力标签供选择器展示
    def _model_capability_labels(
        self,
        route: ProviderRoute | None = None,
    ) -> tuple[str, ...]:
        selected = route if route is not None else self._route_store.active()
        if selected is None:
            return ()
        return (
            "tools" if selected.supports_tools else "no-tools",
            "parallel-tools" if selected.supports_parallel_tools else "serial-tools",
            "images" if selected.supports_images else "text-only",
            f"thinking={selected.thinking}",
        )

    # 启动受检 Provider route 切换，未通过 Doctor 时保持原活动项
    def _select_provider_route(self, route_id: str) -> None:
        if not route_id:
            self._show_provider_routes()
            return
        self.run_worker(
            self._select_provider_route_checked(route_id),
            name="provider_switch",
            exclusive=False,
        )

    # Doctor 成功后才切换后续 turn 使用的活动 route
    async def _select_provider_route_checked(self, route_id: str) -> None:
        try:
            route = await self._configuration.set_active_checked(
                route_id,
                doctor=self._provider_doctor,
            )
        except (RouteStoreError, ConfigurationValidationError) as exc:
            self._show_safe_error("provider-validation", exc)
            return
        self._route = route.id
        self._provider = route.id
        self._model = route.model
        self._models = [route.model]
        self._update_header("ready")
        active = tr(
            "app.provider.active_route",
            self._locale,
            route=escape(route.id),
            model=escape(route.model),
        )
        self._append(
            Static(
                f"[green]{active}[/green]",
                classes="log-line",
            )
        )

    # 启动受检模型切换，避免未探测 model 直接成为活动 route
    def _select_route_model(self, model: str) -> None:
        selected = model.strip()
        if not selected:
            return
        self.run_worker(
            self._select_route_model_checked(selected),
            name="model_switch",
            exclusive=False,
        )

    # Doctor 成功后更新模型字段并持久化与新模型绑定的收据
    async def _select_route_model_checked(self, selected: str) -> None:
        active = self._route_store.active()
        if active is None:
            self._append(
                Static(
                    f"[yellow]{tr('app.provider.no_active', self._locale)}[/yellow]",
                    classes="log-line",
                )
            )
            return
        payload = active.model_dump(mode="python")
        payload["model"] = selected
        payload["doctor_receipt"] = None
        updated = ProviderRoute.model_validate(payload)
        try:
            updated = await self._configuration.save_route_checked(
                updated,
                update=True,
                doctor=self._provider_doctor,
            )
        except ConfigurationValidationError as exc:
            self._show_safe_error("provider-validation", exc)
            return
        self._route = updated.id
        self._model = updated.model
        if selected not in self._models:
            self._models.append(selected)
        self._update_header("ready")
        active_message = tr(
            "app.provider.active_model",
            self._locale,
            route=escape(updated.id),
            model=escape(updated.model),
        )
        self._append(
            Static(
                f"[green]{active_message}[/green]",
                classes="log-line",
            )
        )

    # 对活动 route 执行有界完整探测并展示每个声明能力的真实分项
    async def _show_provider_doctor(self) -> None:
        try:
            route = self._route_store.active()
            if route is None:
                self._append(
                    Static(
                        f"[yellow]{tr('app.provider.no_active', self._locale)}[/yellow]",
                        classes="log-line",
                    )
                )
                return
            credential = self._credential_store.resolve(route.credential_ref)
            result = await self._provider_doctor.check(route, credential)
            if result.status == "ok":
                validated = route.model_copy(
                    update={"doctor_receipt": result.to_receipt(route)}
                )
                self._route_store.update(validated)
            color = "green" if result.status == "ok" else "red"
            status = escape(result.status)
            category = escape(result.category)
            message = escape(result.message)
            basic = escape(result.basic.status)
            capabilities = " · ".join(
                f"{escape(name)}={escape(check.status)}"
                for name, check in sorted(result.capabilities.items())
            )
            title = tr("app.provider.doctor", self._locale)
            capability_summary = tr(
                "app.provider.capabilities",
                self._locale,
                capabilities=capabilities or "not_run",
            )
            self._append(
                Static(
                    f"[bold cyan]{title}[/bold cyan]  [{color}]{status}[/{color}]  "
                    f"{category}\n[dim]basic={basic} · {message} · "
                    f"credential={result.credential_source}[/dim]\n"
                    f"[dim]{capability_summary}[/dim]",
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
        route = get_route_preset(provider.id, model=model)
        if provider.credential_required:
            route = ProviderRoute.model_validate(
                {
                    **route.model_dump(mode="python"),
                    "credential_ref": f"file:{provider.id}",
                }
            )
        exists = any(item.id == route.id for item in self._route_store.list())
        try:
            route = await self._configuration.save_route_checked(
                route,
                secret=api_key or None,
                activate=True,
                update=exists,
                doctor=self._provider_doctor,
            )
        except ConfigurationValidationError as exc:
            self._show_safe_error("provider-validation", exc, action="generic")
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
        configured = tr(
            "app.provider.configured",
            self._locale,
            route=escape(route.id),
            model=escape(route.model),
        )
        self._append(
            Static(
                f"[green]{configured}[/green]",
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
        if not self._config_provider.credential_required:
            self.run_worker(
                self._discover_config_models(None, ""),
                name="config_models",
                exclusive=False,
            )
            return
        self.mount(
            ConfigApiKeyPrompt(self._config_provider, locale=self._locale),
            before="#prompt",
        )

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
        self.mount(
            ProviderPicker(PROVIDER_PRESETS, self._provider, locale=self._locale),
            before="#prompt",
        )

    # 调用 Models API 并在成功后挂载模型选择器
    async def _discover_config_models(
        self,
        prompt: ConfigApiKeyPrompt | None,
        api_key: str,
    ) -> None:
        provider = self._config_provider
        if provider is None:
            return
        try:
            models = await discover_models(provider, api_key)
        except ValueError as exc:
            identifier = self._show_safe_error("model-discovery", exc)
            if prompt is not None:
                prompt.show_error(
                    tr(
                        "configuration.discovery_failed",
                        self._locale,
                        diagnostic_id=identifier,
                    )
                )
            else:
                self._restore_ready_prompt()
            return
        self._pending_config_key = api_key
        self._discovered_config_models = tuple(models)
        if prompt is not None:
            prompt.remove()
        active = self._model if provider.id == self._provider else ""
        candidate_route = get_route_preset(provider.id)
        self.mount(
            ModelPicker(
                models,
                active,
                self._model_capability_labels(candidate_route),
                locale=self._locale,
            ),
            before="#prompt",
        )

    # 创建指定冻结 Preset 的新会话并切换到该会话
    async def _create_and_switch_session(self, preset_id: str = "standard") -> None:
        if self._client is None:
            return
        try:
            created = await self._client.send_command(
                "session.create",
                {"mode": "chat", "preset_id": preset_id},
            )
            await self._load_session(str(created["session_id"]), resume=False)
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("session-create", exc, action="session")
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
                renamed = tr(
                    "app.session.renamed",
                    self._locale,
                    title=escape(self._session_title),
                )
                self._append(
                    Static(
                        f"[green]{renamed}[/green]",
                        classes="log-line",
                    )
                )
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("session-rename", exc, action="session")
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
            self._show_safe_error("session-fork", exc, action="session")
            self._restore_ready_prompt()

    # 创建继承完整历史但冻结为目标 Preset 的关联会话并立即切换
    async def _do_switch_preset(self, preset_id: str) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            result = await self._client.send_command(
                "session.fork",
                {
                    "session_id": self._session_id,
                    "title": "",
                    "preset_id": preset_id,
                },
            )
            session = result.get("session", {})
            forked_id = str(session.get("session_id", ""))
            if not forked_id:
                raise ValueError("preset fork 结果缺少 session_id")
            await self._switch_session(forked_id)
        except (IpcError, RuntimeError, OSError, ValueError) as exc:
            self._show_safe_error("session-preset", exc, action="session")
            self._restore_ready_prompt()

    # 导出当前会话到工作区文件
    async def _do_export_session(self, fmt: str, *, overwrite: bool = False) -> None:
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
            target = Path.cwd() / (filename or f"coderook-session-{self._session_id}.{suffix}")
            if target.exists() and not overwrite:
                notice = tr(
                    "app.session.export_exists",
                    self._locale,
                    path=escape(str(target)),
                    format="json" if format_name == "json" else "md",
                )
                self._append(Static(f"[yellow]{notice}[/yellow]", classes="log-line"))
                return
            target.write_text(content, encoding="utf-8")
            exported = tr(
                "app.session.exported",
                self._locale,
                path=escape(str(target)),
            )
            self._append(
                Static(
                    f"[green]{exported}[/green]",
                    classes="log-line",
                )
            )
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("session-export", exc, action="session")
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
                    f"[green]{tr('app.session.deleted', self._locale)}[/green]  "
                    f"{escape(session_id)}",
                    classes="log-line",
                )
            )
            await self._create_and_switch_session()
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("session-delete", exc, action="session")
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

    # 恢复空闲会话或只读附着活动会话后进入统一加载流程
    async def _switch_session(self, session_id: str) -> None:
        if self._client is None:
            return
        if (
            session_id == self._session_id
            and not self._connection.session_requires_replay(session_id)
        ):
            self._restore_ready_prompt()
            return
        self._pending_rewind = None
        try:
            info, _attached_active = await self._connection.resume_or_attach_session(
                self._client,
                session_id,
            )
            title = str(info.get("title", ""))
            await self._load_session(session_id, resume=True, title=title)
        except (IpcError, RuntimeError, OSError) as exc:
            self._show_safe_error("session-switch", exc, action="session")
            self._restore_ready_prompt()

    # 串行加载目标会话，避免多个 UI 工作线程交错修改当前会话状态
    async def _load_session(
        self,
        session_id: str,
        *,
        resume: bool,
        title: str | None = None,
    ) -> None:
        async with self._session_transition_lock:
            await self._load_session_locked(
                session_id,
                resume=resume,
                title=title,
            )

    # 在会话切换锁内完成历史、订阅、状态与 composer 的一致切换
    async def _load_session_locked(
        self,
        session_id: str,
        *,
        resume: bool,
        title: str | None = None,
    ) -> None:
        if self._client is None:
            return
        self._snapshot_session_composer()
        history = await self._client.send_command(
            "session.get_history",
            {"session_id": session_id},
        )
        state = await self._connection.activate_session(
            session_id,
            lambda: self._prepare_session_view(
                session_id,
                history.get("messages", []),
                resume=resume,
                title=title,
            ),
        )
        await self._refresh_authority()
        await self._refresh_goal_state()
        await self._refresh_session_cost()
        self._reconcile_session_state(state)
        self._restore_session_composer(session_id)
        self._update_header("running" if self._busy else "ready")
        if (
            not self._busy
            and not self._pending_permission_blocks
            and self._pending_question_id is None
        ):
            self._restore_ready_prompt()

    # 保存离开会话时的草稿与待发送图片，避免 composer 跨会话污染
    def _snapshot_session_composer(self) -> None:
        session_id = self._session_id
        if not isinstance(session_id, str) or not session_id:
            return
        prompt = self._prompt()
        self._session_composer_states[session_id] = {
            "draft": prompt.text if prompt is not None else "",
            "attachments": [dict(item) for item in self._pending_image_attachments],
        }

    # 清空旧会话视图并装入目标会话历史，事件补交随后在同一激活阶段完成
    async def _prepare_session_view(
        self,
        session_id: str,
        messages: object,
        *,
        resume: bool,
        title: str | None,
    ) -> None:
        self._break_llm()
        self._clear_plan_review()
        self._clear_user_question()
        self._clear_pending_permissions()
        log_view = self.query_one("#log-view", VerticalScroll)
        await log_view.remove_children()
        self._session_id = session_id
        self._resume_session_id = session_id
        self._history_loaded = True
        self._session_title = title or ""
        self._titled = bool(title and title != "Untitled")
        self._first_user_text = ""
        self._active_run_id = None
        self._busy = False
        self._cancel_requested = False
        self._cancel_armed = False
        self._last_context_pct = 0.0
        self._pending_image_attachments = []
        self._refresh_attachment_strip()
        self._reset_cost_state()
        action = "resumed" if resume else "created"
        label = tr(f"app.session.{action}", self._locale)
        self._append(
            Static(
                f"[bold cyan]{label}[/bold cyan]  [dim]{session_id}[/dim]",
                classes="log-line",
            )
        )
        history_messages = messages if isinstance(messages, list) else []
        self._append_history(history_messages)
        self._resume_deferred_run_results(session_id)

    # 按连接层归并状态恢复活动 run、审批、问题与计划审阅控件
    def _reconcile_session_state(self, state: object) -> None:
        active_run_id = getattr(state, "active_run_id", None)
        self._active_run_id = active_run_id if isinstance(active_run_id, str) else None
        self._busy = self._active_run_id is not None
        raw_permissions = getattr(state, "pending_permissions", {})
        permissions = raw_permissions if isinstance(raw_permissions, dict) else {}
        if set(self._pending_permission_blocks) != set(permissions):
            self._clear_pending_permissions()
        for tool_use_id, event in permissions.items():
            if tool_use_id not in self._pending_permission_blocks and isinstance(event, dict):
                self._handle_event(dict(event))
        question = getattr(state, "pending_question", None)
        if not isinstance(question, dict):
            self._clear_user_question()
        elif question.get("question_id") != self._pending_question_id:
            self._handle_event(dict(question))
        plan = getattr(state, "pending_plan", None)
        if not isinstance(plan, dict):
            self._clear_plan_review()
        elif not self._plan_review_pending:
            self._handle_event(dict(plan))
        prompt = self._prompt()
        if (
            prompt is not None
            and self._busy
            and not self._pending_permission_blocks
            and self._pending_question_id is None
            and not self._plan_review_pending
        ):
            prompt.disabled = False
            prompt.read_only = False
            prompt.border_title = tr("shell.running", self._locale)

    # 恢复目标会话自己的草稿与附件且不覆盖事件恢复出的交互状态
    def _restore_session_composer(self, session_id: str) -> None:
        state = self._session_composer_states.get(session_id, {})
        prompt = self._prompt()
        draft = state.get("draft", "")
        if prompt is not None and isinstance(draft, str):
            prompt.text = draft
        raw_attachments = state.get("attachments", [])
        self._pending_image_attachments = (
            [dict(item) for item in raw_attachments if isinstance(item, dict)]
            if isinstance(raw_attachments, list)
            else []
        )
        self._refresh_attachment_strip()

    # 在 worker 中执行 IPC 发送，使 App 消息泵在 agent 运行期间仍能处理键盘/焦点等消息
    async def _do_send_message(
        self,
        content: str,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        *,
        attachments: list[dict[str, object]] | None = None,
        display_content: str | None = None,
    ) -> None:
        shown_content = display_content or content
        if self._client is None:
            for attachment in attachments or []:
                if attachment not in self._pending_image_attachments:
                    self._pending_image_attachments.append(attachment)
            self._refresh_attachment_strip()
            self._busy = False
            self._active_run_id = None
            self._restore_unsent_draft(
                shown_content,
                tr("app.draft.reconnected", self._locale),
            )
            self._update_header("disconnected")
            self._show_safe_error("submission", "connection lost", action="submission")
            return
        try:
            params: dict[str, object] = {
                "session_id": self._session_id or "",
                "content": content,
                "runtime_mode": runtime_mode.value,
            }
            params["display_content"] = shown_content
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
            self._refresh_attachment_strip()
            self._busy = False
            self._active_run_id = None
            restored = self._restore_unsent_draft(
                shown_content,
                tr("app.draft.failed", self._locale),
            )
            state = "ready" if self._client is not None else "disconnected"
            self._update_header(state)
            if not restored:
                log.warning("unsent draft was not restored because prompt is non-empty")
            self._show_safe_error("submission", e, action="submission")

    # 将用户运行中纠偏发送给当前活动 run
    async def _do_steer(self, run_id: str, content: str) -> None:
        if self._client is None:
            self._restore_unsent_draft(
                content,
                tr("app.draft.steer_failed", self._locale),
            )
            self._show_safe_error("submission", "connection lost", action="submission")
            return
        try:
            await self._client.send_command(
                "run.steer",
                {"run_id": run_id, "content": content},
            )
            prompt = self._prompt()
            if prompt is not None and self._busy:
                prompt.border_title = tr("app.steer.sent", self._locale)
                prompt.focus()
        except (IpcError, RuntimeError, OSError) as exc:
            restored = self._restore_unsent_draft(
                content,
                tr("app.steer.failed", self._locale),
            )
            if not restored:
                log.warning("unsent steer draft was not restored because prompt is non-empty")
            self._show_safe_error("submission", exc, action="submission")

    # 将选项或自由文本答案发送给挂起的结构化问题
    async def _do_answer_question(self, question_id: str, answer: str) -> None:
        if self._client is None:
            self._answering_question = True
            self._restore_unsent_draft(answer, tr("question.prompt", self._locale))
            return
        session_id = self._session_id
        try:
            await self._client.send_command(
                "user_question.respond",
                {"question_id": question_id, "answer": answer},
            )
            if isinstance(session_id, str):
                self._connection.resolve_question(session_id, question_id)
            self._pending_question_id = None
            self._answering_question = False
            prompt = self._prompt()
            if prompt is not None:
                prompt.disabled = False
                prompt.border_title = tr("app.answer.sent", self._locale)
                prompt.focus()
        except (IpcError, RuntimeError, OSError) as exc:
            self._answering_question = True
            self._show_safe_error("submission", exc, action="submission")
            self._restore_unsent_draft(answer, tr("question.prompt", self._locale))

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
                prompt.border_title = tr("question.prompt", self._locale)
                prompt.focus()
            return
        self._answering_question = False
        if prompt is not None:
            prompt.disabled = True
            prompt.border_title = tr("app.answer.sending", self._locale)
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
        plan_session_id = self._plan_session_id
        plan_run_id = self._plan_run_id
        original_request = self._plan_request
        prompt = self._prompt()
        decision = message.decision
        if (
            self._client is None
            or self._session_id is None
            or self._session_id != plan_session_id
            or not isinstance(plan_session_id, str)
            or not isinstance(plan_run_id, str)
            or not plan_run_id
            or message.review.run_id != plan_run_id
            or decision not in {"approve", "revise", "cancel"}
            or (decision == "approve" and prompt is None)
        ):
            self._show_safe_error("plan-response", "invalid plan session", action="session")
            return
        typed_decision = cast(Literal["approve", "revise", "cancel"], decision)
        message.review.disabled = True
        try:
            await ipc_actions.respond_plan(
                self._client,
                plan_session_id,
                plan_run_id,
                typed_decision,
            )
        except IpcActionError as exc:
            if (
                self._plan_review_pending
                and self._plan_session_id == plan_session_id
                and self._plan_run_id == plan_run_id
            ):
                message.review.disabled = False
                if message.review.is_attached:
                    message.review.focus()
            self._show_safe_error("plan-response", exc, action="submission")
            return
        self._connection.resolve_plan(plan_session_id, plan_run_id)
        self._clear_plan_review()
        if decision == "approve":
            assert prompt is not None
            if not await self._ensure_task_ready():
                prompt.text = original_request
                return
            await self._select_runtime_mode(RuntimeMode.ACT, announce=False)
            self._begin_message(
                prompt,
                "Implement the approved plan from the immediately preceding planning turn. "
                "Re-check repository state before editing and report any required deviation."
                f"\n\nOriginal user request:\n{original_request}",
                RuntimeMode.ACT,
                visible_content=tr("app.plan.approve_task", self._locale),
            )
            return
        self._restore_ready_prompt()
        if decision == "revise":
            await self._select_runtime_mode(RuntimeMode.PLAN, announce=False)
            prompt = self._prompt()
            if prompt is not None:
                prompt.text = "/plan "
                prompt.move_cursor(prompt.document.end)
                prompt.border_title = tr("app.plan.feedback", self._locale)
            self._update_header("plan")
        else:
            await self._select_runtime_mode(RuntimeMode.ACT, announce=False)
            self._append(
                Static(
                    f"[dim]{tr('app.plan.cancelled', self._locale)}[/dim]",
                    classes="log-line",
                )
            )
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
                    if isinstance(self._session_id, str):
                        self._connection.resolve_permission(self._session_id, tool_use_id)
                except (IpcError, RuntimeError, OSError):
                    pass
            if not self._pending_permission_blocks:
                p = self._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = tr("shell.connected", self._locale)
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

    # 将恢复会话的历史消息与工具结果对账后转换为简洁且状态真实的 TUI 块
    def _append_history(self, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        tool_results: dict[str, dict[str, Any]] = {}
        for message in messages:
            content = message.get("content", "")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id", ""))
                if tool_use_id:
                    tool_results[tool_use_id] = block
        divider = tr("app.session.history_divider", self._locale)
        self._append(Static(f"[dim]── {divider} ──[/dim]", classes="log-line"))
        for message in messages:
            role = str(message.get("role", ""))
            content = message.get("content", "")
            if isinstance(content, str):
                if role == "user":
                    self._append(Static(escape(content), classes="user-turn"))
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
                    tool_use_id = str(block.get("id", ""))
                    result = tool_results.get(tool_use_id)
                    failed = bool(result and result.get("is_error"))
                    action = escape(
                        _tool_action_text(
                            str(block.get("name", "")),
                            params,
                            finished=not failed,
                            locale=self._locale,
                        )
                    )
                    marker = "[red]×[/red]" if failed else "[green]✓[/green]"
                    if failed:
                        action = escape(
                            _tool_failure_text(
                                str(block.get("name", "")),
                                params,
                                locale=self._locale,
                            )
                        )
                    self._append(
                        Static(
                            f"{marker} [#aab2be]{action}[/#aab2be]",
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
        self._plan_run_id = None
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

    # 移除旧会话审批控件与缓存，防止对新会话发送错误决定
    def _clear_pending_permissions(self) -> None:
        for block in list(self._pending_permission_blocks.values()):
            try:
                block.remove()
            except Exception:
                pass
        self._pending_permission_blocks.clear()
        try:
            for select in list(self.query(PermissionSelect)):
                select.remove()
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

    # 从 Runtime durable 投影恢复 session 总成本，确保切换或重启 TUI 后顶栏不归零
    async def _refresh_session_cost(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            context = await ipc_actions.get_context(self._client, self._session_id)
            usage = context.get("session_usage", {})
            if not isinstance(usage, dict):
                return
            known = usage.get("known_estimated_cost_usd", 0.0)
            self._cost_total = (
                float(known)
                if isinstance(known, (int, float)) and not isinstance(known, bool)
                else 0.0
            )
            self._update_header(self._header_state)
        except (IpcActionError, ValueError, TypeError):
            log.warning("failed to restore durable session cost", exc_info=True)

    # 展示包含子 Agent 的持久 session 用量，未知模型价格时明确标注已知小计
    async def _show_durable_cost_breakdown(self) -> None:
        if self._client is None or self._session_id is None:
            return
        try:
            context = await ipc_actions.get_context(self._client, self._session_id)
            usage = context.get("session_usage", {})
            if not isinstance(usage, dict):
                raise ValueError("runtime returned invalid session usage")
            known = usage.get("known_estimated_cost_usd", 0.0)
            known_cost = (
                float(known)
                if isinstance(known, (int, float)) and not isinstance(known, bool)
                else 0.0
            )
            self._cost_total = known_cost
            models = ", ".join(str(item) for item in usage.get("models", [])) or "none"
            lines = [f"[bold cyan]{tr('app.cost.title', self._locale)}[/bold cyan]"]
            if usage.get("cost_status") == "unknown":
                subtotal = tr(
                    "app.cost.known_subtotal",
                    self._locale,
                    cost=format_cost(known_cost),
                )
                lines.append(
                    f"  [yellow]{subtotal}[/yellow]"
                )
            else:
                total = tr(
                    "app.cost.total",
                    self._locale,
                    cost=format_cost(known_cost),
                )
                lines.append(f"  [bold]{total}[/bold]")
            lines.append(
                "  [dim]"
                f"in={int(usage.get('input_tokens', 0))} "
                f"out={int(usage.get('output_tokens', 0))} "
                f"cache_read={int(usage.get('cache_read_input_tokens', 0))} "
                f"cache_write={int(usage.get('cache_creation_input_tokens', 0))}[/dim]"
            )
            lines.append(
                f"  [dim]turns={int(usage.get('turn_count', 0))} "
                f"workers={int(usage.get('worker_count', 0))} "
                f"worker_tokens={int(usage.get('worker_token_usage', 0))}[/dim]"
            )
            lines.append(f"  [dim]models={escape(models)}[/dim]")
            lines.append(f"[dim]{tr('app.cost.note', self._locale)}[/dim]")
            self._append(Static("\n".join(lines), classes="log-line"))
            self._update_header(self._header_state)
        except (IpcActionError, ValueError, TypeError) as exc:
            self._show_safe_error("cost", exc, action="inspection")
        finally:
            self._restore_ready_prompt()

    # 按 80/100/140 列产品门槛生成主动收缩而非依赖终端硬截断的顶栏
    def _render_responsive_header(self, state: str, width: int) -> str:
        color = {
            "ready": "green",
            "running": "yellow",
            "planning": "cyan",
            "plan": "cyan",
            "plan ready": "cyan",
            "disconnected": "red",
            "connecting": "dim",
        }.get(state, "dim")
        state_label = tr(f"header.state.{state}", self._locale)
        repo = self._workspace.name or "-"
        model = self._model or "-"
        phase_labels = {
            "understanding": "理解" if self._locale == "zh-CN" else "Understanding",
            "exploring": "探索" if self._locale == "zh-CN" else "Exploring",
            "planning": "规划" if self._locale == "zh-CN" else "Planning",
            "waiting_confirmation": "等待确认" if self._locale == "zh-CN" else "Waiting",
            "executing": "执行" if self._locale == "zh-CN" else "Editing",
            "verifying": "验证" if self._locale == "zh-CN" else "Verifying",
            "reviewing": "审查" if self._locale == "zh-CN" else "Reviewing",
            "completed": "完成" if self._locale == "zh-CN" else "Complete",
            "failed": "失败" if self._locale == "zh-CN" else "Failed",
            "interrupted": "中断" if self._locale == "zh-CN" else "Interrupted",
        }
        phase = phase_labels.get(self._run_phase, state_label)
        progress = (
            f" {self._run_phase_current}/{self._run_phase_total}"
            if self._run_phase_total
            else ""
        )
        model_part = (
            f" · [cyan]{escape(_preview(model, 24 if width >= 100 else 13))}[/cyan]"
            if width >= 80
            else ""
        )
        return (
            f"[bold]CodeRook[/bold] · {escape(_preview(repo, 20))}{model_part}"
            f"    [{color}]{escape(phase)}{progress}[/{color}]"
        )

    # 刷新 Composer 上方的模式、权限、沙箱、上下文、成本和消息队列状态
    def _update_status_bar(self) -> None:
        try:
            status_bar = self.query_one("#status-bar", Static)
        except NoMatches:
            return
        sandbox_kind = str(self._sandbox.get("kind") or "none")
        sandbox_reason = str(self._sandbox.get("reason") or "")
        if sandbox_kind == "none" and "not been detected" in sandbox_reason:
            sandbox_state = (
                "Sandbox 检查中" if self._locale == "zh-CN" else "Checking sandbox"
            )
        elif self._sandbox.get("available") and sandbox_kind == "windows_acl":
            sandbox_state = (
                "Windows 部分沙箱"
                if self._locale == "zh-CN"
                else "Windows partial sandbox"
            )
        elif self._sandbox.get("available"):
            sandbox_state = sandbox_kind
        elif sandbox_kind == "windows_none":
            sandbox_state = (
                "Windows 无 OS 沙箱" if self._locale == "zh-CN" else "no OS sandbox"
            )
        else:
            sandbox_state = (
                "Sandbox 不可用" if self._locale == "zh-CN" else "Sandbox unavailable"
            )
        queue = (
            f" · queue {len(self._queued_messages)}"
            if self._queued_messages
            else ""
        )
        status_bar.update(
            f"[blue]{self._input_runtime_mode.value.upper()}[/blue] · "
            f"[magenta]{self._authority_preset}[/magenta] · {escape(sandbox_state)} · "
            f"ctx {self._last_context_pct * 100:.0f}% · ${self._cost_total:.4f}{queue}"
        )

    # 根据连接和运行状态刷新顶部标题，并使用当前终端实际列宽选择信息层级
    def _update_header(self, state: str) -> None:
        self._header_state = state
        try:
            header = self.query_one("#header", Label)
        except NoMatches:
            return
        width = self.size.width if self.size.width > 0 else 100
        header.update(self._render_responsive_header(state, width))
        self._update_status_bar()

    # 终端尺寸变化时立即重算顶栏而不是等待下一条运行事件
    def on_resize(self, _event: events.Resize) -> None:
        self._update_header(self._header_state)

    # 根据事件 type 路由到对应渲染逻辑；捕获异常防止 socket loop 因单个事件崩溃
    def _handle_event(self, event: dict[str, Any]) -> None:
        try:
            self._run_evidence.consume(event)
            render_event(self, event)
            if (
                event.get("type")
                in {
                    "session.waiting_for_input",
                    "session.interrupted",
                    "goal.continue_decision",
                }
                and event.get("session_id") == self._session_id
            ):
                self.call_later(self._refresh_goal_state)
        except Exception:
            log.exception("_handle_event crashed  event_type=%s", event.get("type", "?"))

    # 独立处理 daemon 全局事件，审计故障只展示脱敏诊断且不污染当前任务时间线
    def _handle_daemon_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "audit.degraded":
            return
        identifier = str(event.get("diagnostic_id", "") or "AUD-UNKNOWN")
        self._append(
            SafeErrorCard(
                "audit_degraded",
                identifier,
                action="audit",
                locale=self._locale,
            )
        )


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

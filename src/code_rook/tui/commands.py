"""斜杠命令数据驱动注册表。

`BUILTIN_SLASH_COMMANDS` 是内建命令的唯一事实来源：补全弹窗、帮助文案与
`on_chat_text_area_submitted` 的分发都消费它，避免"补全列表 + if 分发"两处
各自维护。动态 skill 名不在注册表内，补全弹窗按现有逻辑在 App 侧追加。

每个命令的 handler 接收 `(app, text_area, content)`，其中 `app` 为
`CodeRookTuiApp` 实例、`text_area` 为触发命令的输入框、`content` 为原始输入
（含 `/名称` 与参数）。handler 通过 `app.xxx` 访问 App 的方法与状态，保持与
旧版 if 分支逐字等价（含 busy/连接守卫与提示文案）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from rich.markup import escape
from textual.css.query import NoMatches
from textual.widgets import Static

from code_rook.core.authority import RuntimeMode, WorkspaceTrust
from code_rook.core.llm.provider_presets import PROVIDER_PRESETS
from code_rook.tui.widgets.input import ChatTextArea
from code_rook.tui.widgets.permission import PermissionModePicker
from code_rook.tui.widgets.selectors import ModelPicker, ProviderPicker


@dataclass(frozen=True)
class SlashCommand:
    """一个斜杠命令：名称、补全/帮助共用文案，以及是否依赖 Core 连接。"""

    name: str
    description: str
    needs_connection: bool
    handler: Callable[[Any, ChatTextArea, str], Awaitable[None]]


async def _cmd_help(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._show_help()


async def _cmd_sessions(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is not None and not app._busy:
        ta.disabled = True
        ta.border_title = "正在加载会话"
        app.run_worker(app._show_session_picker(), name="session_picker", exclusive=False)


async def _cmd_new(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is not None and not app._busy:
        ta.disabled = True
        ta.border_title = "正在创建会话"
        app.run_worker(
            app._create_and_switch_session(), name="new_session", exclusive=False
        )


async def _cmd_rename(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    title = content.removeprefix("/rename").strip()
    if not title:
        app._append(Static("[yellow]用法：/rename <新标题>[/yellow]", classes="log-line"))
    elif app._client is not None and app._session_id is not None and not app._busy:
        ta.disabled = True
        ta.border_title = "正在重命名会话"
        app.run_worker(
            app._do_rename_session(title), name="rename_session", exclusive=False
        )


async def _cmd_fork(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None or app._busy:
        app._append(
            Static("[yellow]Core 未连接或任务运行中，稍后再试[/yellow]", classes="log-line")
        )
        return
    title = content.removeprefix("/fork").strip()
    ta.disabled = True
    ta.border_title = "正在复制会话"
    app.run_worker(app._do_fork_session(title), name="fork_session", exclusive=False)


async def _cmd_export(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        app._append(Static("[yellow]Core 未连接[/yellow]", classes="log-line"))
        return
    fmt = content.removeprefix("/export").strip().lower()
    if fmt not in {"", "md", "json", "markdown"}:
        app._append(Static("[yellow]用法：/export [md|json][/yellow]", classes="log-line"))
        return
    ta.disabled = True
    ta.border_title = "正在导出会话"
    app.run_worker(app._do_export_session(fmt), name="export_session", exclusive=False)


async def _cmd_delete(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None or app._busy:
        app._append(
            Static("[yellow]Core 未连接或任务运行中，稍后再试[/yellow]", classes="log-line")
        )
        return
    if "--yes" not in content:
        app._append(
            Static(
                f"[yellow]将删除当前会话 {escape(app._session_id)} 及其全部历史，"
                "确认请输入 /delete --yes[/yellow]",
                classes="log-line",
            )
        )
        return
    ta.disabled = True
    ta.border_title = "正在删除会话"
    app.run_worker(app._do_delete_session(), name="delete_session", exclusive=False)


async def _cmd_provider(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if content == "/provider":
        app._show_provider_routes()
        return
    if app._busy:
        app._append(
            Static(
                "[yellow]当前任务运行中，结束后再切换 Provider"
                "（Ctrl+C 可取消任务）[/yellow]",
                classes="log-line",
            )
        )
        return
    app._select_provider_route(content.removeprefix("/provider ").strip())


async def _cmd_model(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._busy:
        app._append(
            Static(
                "[yellow]当前任务运行中，结束后再切换模型（Ctrl+C 可取消任务）[/yellow]",
                classes="log-line",
            )
        )
        return
    if content == "/model":
        ta.disabled = True
        ta.border_title = "选择模型"
        app.mount(ModelPicker(app._models, app._model), before="#prompt")
        return
    selected = content.removeprefix("/model ").strip()
    if selected.startswith("add "):
        selected = selected.removeprefix("add ").strip()
    if selected:
        app._select_route_model(selected)
    else:
        app._append(
            Static(
                "[yellow]用法：/model <模型 ID> 或 /model add <模型 ID>[/yellow]",
                classes="log-line",
            )
        )


async def _cmd_doctor(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._busy:
        app._append(
            Static(
                "[yellow]当前任务运行中，结束后再运行诊断（Ctrl+C 可取消任务）[/yellow]",
                classes="log-line",
            )
        )
        return
    ta.disabled = True
    ta.border_title = "正在诊断 Provider"
    app.run_worker(app._show_provider_doctor(), name="provider_doctor", exclusive=False)


async def _cmd_config(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._busy:
        app._append(
            Static(
                "[yellow]当前任务运行中，结束后再修改 LLM 配置"
                "（Ctrl+C 可取消任务）[/yellow]",
                classes="log-line",
            )
        )
        return
    ta.disabled = True
    ta.border_title = "选择 API 平台"
    app.mount(ProviderPicker(PROVIDER_PRESETS, app._provider), before="#prompt")


async def _cmd_compact(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is not None and app._session_id is not None and not app._busy:
        app.run_worker(app._do_compact(), name="compact", exclusive=False)


async def _cmd_copy(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._copy_last_response()


async def _cmd_plan(app: Any, ta: ChatTextArea, content: str) -> None:
    if content == "/plan":
        ta.text = ""
        await app._select_runtime_mode(RuntimeMode.PLAN, announce=False)
        return
    task = content.removeprefix("/plan ").strip()
    if not task:
        ta.text = ""
        ta.border_title = "规划模式"
        app._input_runtime_mode = RuntimeMode.PLAN
        return
    app._begin_message(ta, task, RuntimeMode.PLAN)


async def _cmd_mode(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if content == "/mode":
        app._append(
            Static(
                f"[bold cyan]工作模式[/bold cyan]  {app._input_runtime_mode.value}"
                "  [dim]用法：/mode plan|act|operate[/dim]",
                classes="log-line",
            )
        )
        return
    raw_mode = content.removeprefix("/mode ").strip()
    try:
        mode = RuntimeMode(raw_mode)
    except ValueError:
        app._append(
            Static("[yellow]用法：/mode plan|act|operate[/yellow]", classes="log-line")
        )
    else:
        await app._select_runtime_mode(mode)


async def _cmd_permissions(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if content == "/permissions":
        ta.disabled = True
        ta.border_title = "选择权限模式"
        try:
            app.query_one(PermissionModePicker).remove()
        except NoMatches:
            pass
        app.mount(PermissionModePicker(app._authority_preset), before="#prompt")
        return
    requested = content.removeprefix("/permissions ").strip().replace("-", "_")
    aliases = {
        "ask": "ask",
        "auto_review": "accept_edits",
        "accept_edits": "accept_edits",
        "full_access": "full_access",
    }
    preset = aliases.get(requested)
    if preset is None:
        app._append(
            Static(
                "[yellow]用法：/permissions ask|auto-review|full-access[/yellow]",
                classes="log-line",
            )
        )
    else:
        await app._select_authority_preset(preset)


async def _cmd_trust(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    action = content.removeprefix("/trust").strip() or "status"
    if action == "status":
        app._show_trust_status()
    elif action in {"grant", "revoke"}:
        trust = (
            WorkspaceTrust.TRUSTED
            if action == "grant"
            else WorkspaceTrust.UNTRUSTED
        )
        await app._set_workspace_trust(trust)
    else:
        app._append(
            Static("[yellow]用法：/trust status|grant|revoke[/yellow]", classes="log-line")
        )


async def _cmd_sandbox(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._show_sandbox_status()


def _make_viewer(
    name: str, target: str, needs_session: bool
) -> Callable[[Any, ChatTextArea, str], Awaitable[None]]:
    """为 tasks/workers/diff/rewind/context 生成共享的只读查看器 handler。"""

    async def handler(app: Any, ta: ChatTextArea, content: str) -> None:
        ta.text = ""
        if app._client is None or (needs_session and app._session_id is None):
            app._append(Static("[yellow]Core 未连接[/yellow]", classes="log-line"))
            return
        ta.disabled = True
        ta.border_title = f"正在加载 {content}"
        app.run_worker(getattr(app, target)(), name=f"view_{name}", exclusive=False)

    return handler


async def _cmd_workflow(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        app._append(Static("[yellow]Core 未连接[/yellow]", classes="log-line"))
        return
    ta.disabled = True
    ta.border_title = f"正在加载 {content}"
    arg = content.removeprefix("/workflow").strip()
    if arg.startswith("start "):
        workflow_path = arg.removeprefix("start ").strip()
        app.run_worker(
            app._start_workflow_file(workflow_path),
            name="start_workflow",
            exclusive=False,
        )
    else:
        app.run_worker(app._show_workflow(arg), name="view_workflow", exclusive=False)


async def _cmd_turn(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        app._append(Static("[yellow]Core 未连接[/yellow]", classes="log-line"))
        return
    ta.disabled = True
    ta.border_title = f"正在加载 {content}"
    turn_id = content.removeprefix("/turn").strip()
    app.run_worker(app._show_turn(turn_id), name="view_turn", exclusive=False)


async def _cmd_cost(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._show_cost_breakdown()


async def _cmd_skills(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._handle_skills_command(content)


# 内建命令的唯一事实来源；顺序即补全弹窗展示顺序
BUILTIN_SLASH_COMMANDS: list[SlashCommand] = [
    SlashCommand("help", "显示键位与全部命令", False, _cmd_help),
    SlashCommand("sessions", "打开会话选择器（输入即过滤）", True, _cmd_sessions),
    SlashCommand("new", "新建会话", True, _cmd_new),
    SlashCommand("rename", "重命名当前会话：/rename <标题>", True, _cmd_rename),
    SlashCommand("fork", "复制当前会话为分支：/fork [标题]", True, _cmd_fork),
    SlashCommand("export", "导出当前会话：/export [md|json]", True, _cmd_export),
    SlashCommand("delete", "删除当前会话（需 --yes 确认）", True, _cmd_delete),
    SlashCommand("provider", "查看或切换 Provider route", False, _cmd_provider),
    SlashCommand("model", "查看或切换模型", False, _cmd_model),
    SlashCommand("doctor", "诊断活动 Provider route", False, _cmd_doctor),
    SlashCommand("config", "更换 LLM API、模型或密钥", False, _cmd_config),
    SlashCommand("compact", "手动压缩上下文", True, _cmd_compact),
    SlashCommand("copy", "复制上一条回复", False, _cmd_copy),
    SlashCommand("plan", "只读规划并审阅后再实施：/plan [任务]", False, _cmd_plan),
    SlashCommand("mode", "查看或切换工作模式：plan|act|operate", False, _cmd_mode),
    SlashCommand("permissions", "查看或切换权限模式", False, _cmd_permissions),
    SlashCommand("trust", "查看或授予/撤销工作区信任", False, _cmd_trust),
    SlashCommand("sandbox", "查看 OS 隔离能力（仅探测）", False, _cmd_sandbox),
    SlashCommand(
        "tasks", "查看最近一次 run 的任务", True, _make_viewer("tasks", "_show_tasks", True)
    ),
    SlashCommand(
        "workers",
        "查看全部持久 Worker 与 Fleet",
        True,
        _make_viewer("workers", "_show_workers", True),
    ),
    SlashCommand("workflow", "查看、启动或检查 workflow", True, _cmd_workflow),
    SlashCommand(
        "diff", "查看工作区改动", True, _make_viewer("diff", "_show_diff", True)
    ),
    SlashCommand(
        "rewind",
        "从安全恢复点回滚文件",
        True,
        _make_viewer("rewind", "_show_rewind_picker", True),
    ),
    SlashCommand(
        "context",
        "查看上下文占用与用量",
        True,
        _make_viewer("context", "_show_context", True),
    ),
    SlashCommand("cost", "查看本会话成本分解与缓存节省", False, _cmd_cost),
    SlashCommand("turn", "检查 route、用量、审批与收据", True, _cmd_turn),
    SlashCommand("skills", "列出、查看、安装或删除 skills", False, _cmd_skills),
]


def match_slash_command(content: str) -> SlashCommand | None:
    """按 `/name` 或 `/name <args>` 精确匹配单个命令；未知前缀返回 None。"""
    for cmd in BUILTIN_SLASH_COMMANDS:
        if content == f"/{cmd.name}" or content.startswith(f"/{cmd.name} "):
            return cmd
    return None
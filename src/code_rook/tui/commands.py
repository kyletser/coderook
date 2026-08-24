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

import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from rich.markup import escape
from textual.css.query import NoMatches
from textual.widgets import Static

from code_rook.core.authority import RuntimeMode, WorkspaceTrust
from code_rook.core.bus.commands import GoalCreateCommand
from code_rook.core.llm.provider_presets import PROVIDER_PRESETS
from code_rook.tui.product import tr
from code_rook.tui.widgets.input import ChatTextArea
from code_rook.tui.widgets.permission import PermissionModePicker
from code_rook.tui.widgets.selectors import ModelPicker, ProviderPicker


@dataclass(frozen=True)
class SlashCommand:
    """一个斜杠命令：名称、补全/帮助共用文案、是否依赖 Core 连接，以及可选 usage 与参数补全候选。"""

    name: str
    description: str
    needs_connection: bool
    handler: Callable[[Any, ChatTextArea, str], Awaitable[None]]
    usage: str = ""
    arg_candidates: tuple[str, ...] = ()
    labs: bool = False


_COMMAND_CATEGORIES: dict[str, str] = {
    "help": "task",
    "copy": "task",
    "compact": "task",
    "plan": "task",
    "goal": "task",
    "mode": "task",
    "tasks": "task",
    "sessions": "session",
    "new": "session",
    "rename": "session",
    "fork": "session",
    "export": "session",
    "delete": "session",
    "history": "session",
    "language": "session",
    "attachments": "session",
    "changes": "review",
    "diff": "review",
    "review": "review",
    "stage": "review",
    "commit": "review",
    "rewind": "review",
    "turn": "review",
    "context": "review",
    "cost": "review",
    "provider": "model",
    "model": "model",
    "doctor": "model",
    "config": "model",
    "permissions": "security",
    "trust": "security",
    "sandbox": "security",
    "skills": "extension",
    "mcp": "extension",
    "memory": "extension",
    "workers": "extension",
    "jobs": "extension",
    "artifacts": "extension",
    "workflow": "labs",
    "hooks": "labs",
}

_DIRECT_PALETTE_COMMANDS = {
    "help",
    "sessions",
    "new",
    "config",
    "doctor",
    "compact",
    "copy",
    "changes",
    "diff",
    "sandbox",
    "tasks",
    "workers",
    "context",
    "cost",
    "attachments",
    "mcp",
    "jobs",
}

_COMMON_PALETTE_PRIORITY = {
    "changes": 0,
    "plan": 1,
    "sessions": 2,
    "model": 3,
    "config": 4,
    "review": 5,
}


# 返回命令面板使用的稳定产品类别
def command_category(name: str) -> str:
    return _COMMAND_CATEGORIES.get(name, "extension")


# 判断无参数命令是否可从 Ctrl+P 直接触发
def command_palette_direct(name: str) -> bool:
    return name in _DIRECT_PALETTE_COMMANDS


# 返回命令面板排序权重，常用入口固定置顶
def command_palette_priority(name: str) -> int:
    return _COMMON_PALETTE_PRIORITY.get(name, 100)


# 读取宿主 App 当前界面语言，测试替身缺失时回退中文
def _locale(app: Any) -> str:
    return str(getattr(app, "_locale", "zh-CN"))


# 追加一条集中式黄色命令提示
def _warn(app: Any, key: str, **values: object) -> None:
    app._append(
        Static(
            f"[yellow]{tr(key, _locale(app), **values)}[/yellow]",
            classes="log-line",
        )
    )


# 设置命令执行期间的集中式 composer 状态
def _progress(app: Any, ta: ChatTextArea, key: str, **values: object) -> None:
    ta.border_title = tr(key, _locale(app), **values)


async def _cmd_help(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._show_help()


async def _cmd_sessions(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is not None and not app._busy:
        ta.disabled = True
        _progress(app, ta, "cmd.loading", target=tr("selector.sessions.title", _locale(app)))
        app.run_worker(app._show_session_picker(), name="session_picker", exclusive=False)


async def _cmd_new(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is not None and not app._busy:
        ta.disabled = True
        _progress(app, ta, "cmd.session.creating")
        app.run_worker(
            app._create_and_switch_session(), name="new_session", exclusive=False
        )


async def _cmd_rename(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    title = content.removeprefix("/rename").strip()
    if not title:
        _warn(app, "cmd.usage", usage="/rename <new-title>")
    elif app._client is not None and app._session_id is not None and not app._busy:
        ta.disabled = True
        _progress(app, ta, "cmd.session.renaming")
        app.run_worker(
            app._do_rename_session(title), name="rename_session", exclusive=False
        )


async def _cmd_fork(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None or app._busy:
        _warn(app, "cmd.core_busy")
        return
    title = content.removeprefix("/fork").strip()
    ta.disabled = True
    _progress(app, ta, "cmd.session.forking")
    app.run_worker(app._do_fork_session(title), name="fork_session", exclusive=False)


async def _cmd_export(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        _warn(app, "cmd.core_disconnected")
        return
    args = content.removeprefix("/export").strip().lower().split()
    flags = {item for item in args if item.startswith("--")}
    formats = [item for item in args if not item.startswith("--")]
    if (
        len(formats) > 1
        or (formats and formats[0] not in {"md", "json", "markdown"})
        or not flags.issubset({"--force", "--yes"})
        or ("--yes" in flags and "--force" not in flags)
    ):
        _warn(app, "cmd.session.export_usage")
        return
    fmt = formats[0] if formats else ""
    overwrite = {"--force", "--yes"}.issubset(flags)
    ta.disabled = True
    _progress(app, ta, "cmd.session.exporting")
    app.run_worker(
        app._do_export_session(fmt, overwrite=overwrite),
        name="export_session",
        exclusive=False,
    )


async def _cmd_delete(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None or app._busy:
        _warn(app, "cmd.core_busy")
        return
    if "--yes" not in content:
        _warn(
            app,
            "cmd.session.delete_confirm",
            session=escape(app._session_id),
        )
        return
    ta.disabled = True
    _progress(app, ta, "cmd.session.deleting")
    app.run_worker(app._do_delete_session(), name="delete_session", exclusive=False)


async def _cmd_provider(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if content == "/provider":
        app._show_provider_routes()
        return
    if app._busy:
        _warn(app, "cmd.provider.busy")
        return
    app._select_provider_route(content.removeprefix("/provider ").strip())


async def _cmd_model(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._busy:
        _warn(app, "cmd.model.busy")
        return
    if content == "/model":
        ta.disabled = True
        _progress(app, ta, "cmd.model.select")
        app.mount(
            ModelPicker(
                app._models,
                app._model,
                app._model_capability_labels(),
                locale=_locale(app),
            ),
            before="#prompt",
        )
        return
    selected = content.removeprefix("/model ").strip()
    if selected.startswith("add "):
        selected = selected.removeprefix("add ").strip()
    if selected:
        app._select_route_model(selected)
    else:
        _warn(app, "cmd.usage", usage="/model <model-id> | /model add <model-id>")


async def _cmd_doctor(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._busy:
        _warn(app, "cmd.doctor.busy")
        return
    ta.disabled = True
    _progress(app, ta, "cmd.doctor.running")
    app.run_worker(app._show_provider_doctor(), name="provider_doctor", exclusive=False)


async def _cmd_config(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._busy:
        _warn(app, "cmd.config.busy")
        return
    ta.disabled = True
    _progress(app, ta, "cmd.config.select")
    app.mount(
        ProviderPicker(PROVIDER_PRESETS, app._provider, locale=_locale(app)),
        before="#prompt",
    )


async def _cmd_compact(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is not None and app._session_id is not None and not app._busy:
        app.run_worker(app._do_compact(), name="compact", exclusive=False)


async def _cmd_copy(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._copy_last_response()


# 查看、开关或清空当前工作区输入历史
async def _cmd_history(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._handle_history_command(content.removeprefix("/history").strip())


# 查看或切换中英文界面语言
async def _cmd_language(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._handle_language_command(content.removeprefix("/language").strip())


# 查看、移除或清空当前 composer 的待发送图片
async def _cmd_attachments(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._handle_attachments_command(content.removeprefix("/attachments").strip())


# 打开 Rewind 预览或执行已预览摘要的第二步显式确认
async def _cmd_rewind(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        _warn(app, "cmd.core_disconnected")
        return
    argument = content.removeprefix("/rewind").strip()
    ta.disabled = True
    if argument == "--yes":
        _progress(app, ta, "cmd.rewind.checking")
        app.run_worker(app._confirm_rewind(), name="confirm_rewind", exclusive=False)
        return
    if argument:
        _warn(app, "cmd.usage", usage="/rewind | /rewind --yes")
        app._restore_ready_prompt()
        return
    _progress(app, ta, "cmd.loading", target="/rewind")
    app.run_worker(app._show_rewind_picker(), name="view_rewind", exclusive=False)


# 启动只读复审，让结果卡提供直接可执行的审查入口
async def _cmd_review(app: Any, ta: ChatTextArea, content: str) -> None:
    if app._client is None or app._session_id is None or app._busy:
        _warn(app, "cmd.core_busy")
        return
    focus = content.removeprefix("/review").strip()
    request = tr("cmd.review.request", _locale(app))
    if focus:
        request += f"\n\nReview focus:\n{focus}"
    app._begin_message(
        ta,
        request,
        RuntimeMode.PLAN,
        visible_content=tr("review.visible", _locale(app))
        + (f" · {focus}" if focus else ""),
    )


async def _cmd_plan(app: Any, ta: ChatTextArea, content: str) -> None:
    if content == "/plan":
        ta.text = ""
        await app._select_runtime_mode(RuntimeMode.PLAN, announce=False)
        return
    task = content.removeprefix("/plan ").strip()
    if not task:
        ta.text = ""
        ta.border_title = tr("shell.plan", _locale(app))
        app._input_runtime_mode = RuntimeMode.PLAN
        return
    app._begin_message(ta, task, RuntimeMode.PLAN)


# 将 Goal 创建参数解析为经过 typed IPC 模型校验的安全参数
def _parse_goal_create_args(
    raw: str,
    session_id: str,
    *,
    locale: str = "zh-CN",
) -> tuple[str, dict[str, object]]:
    try:
        lexer = shlex.shlex(raw, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""
        parts = list(lexer)
    except ValueError as exc:
        raise ValueError(tr("cmd.goal.quote", locale, error=exc)) from exc
    objective_parts: list[str] = []
    criteria: list[str] = []
    constraints: list[str] = []
    token_budget: int | None = None
    auto_continue = True
    max_auto_turns = 3
    max_wall_seconds = 1800
    seen: set[str] = set()
    index = 0
    positional_only = False
    value_options = {
        "--token-budget": "token_budget",
        "--max-auto-turns": "max_auto_turns",
        "--max-wall-seconds": "max_wall_seconds",
        "--criterion": "completion_criteria",
        "--constraint": "constraints",
    }
    while index < len(parts):
        token = parts[index]
        if not positional_only and token == "--":
            positional_only = True
            index += 1
            continue
        if not positional_only and token in {"--auto-continue", "--no-auto-continue"}:
            if "auto_continue" in seen:
                raise ValueError(tr("cmd.goal.auto_once", locale))
            seen.add("auto_continue")
            auto_continue = token == "--auto-continue"
            index += 1
            continue
        if not positional_only and token in value_options:
            option = value_options[token]
            if index + 1 >= len(parts):
                raise ValueError(tr("cmd.goal.missing_value", locale, option=token))
            option_value = parts[index + 1].strip()
            if not option_value or option_value.startswith("--"):
                raise ValueError(
                    tr("cmd.goal.missing_valid_value", locale, option=token)
                )
            if option in {"completion_criteria", "constraints"}:
                target = criteria if option == "completion_criteria" else constraints
                target.append(option_value)
            else:
                if option in seen:
                    raise ValueError(tr("cmd.goal.once", locale, option=token))
                if not option_value.isascii() or not option_value.isdecimal():
                    raise ValueError(tr("cmd.goal.positive", locale, option=token))
                seen.add(option)
                parsed_value = int(option_value)
                if option == "token_budget":
                    token_budget = parsed_value
                elif option == "max_auto_turns":
                    max_auto_turns = parsed_value
                else:
                    max_wall_seconds = parsed_value
            index += 2
            continue
        if not positional_only and token.startswith("--"):
            raise ValueError(tr("cmd.goal.unknown", locale, option=token))
        objective_parts.append(token)
        index += 1
    objective = " ".join(objective_parts).strip()
    try:
        command = GoalCreateCommand(
            session_id=session_id,
            objective=objective,
            token_budget=token_budget,
            auto_continue=auto_continue,
            max_auto_turns=max_auto_turns,
            max_wall_seconds=max_wall_seconds,
            constraints=constraints,
            completion_criteria=criteria,
        )
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        field = ".".join(str(item) for item in first.get("loc", ()))
        message = str(first.get("msg", tr("cmd.goal.invalid_value", locale)))
        prefix = f"{field}: " if field else ""
        raise ValueError(prefix + message) from exc
    return objective, command.model_dump(exclude={"type"})


# 解析持久 Goal 子命令，并把长运行操作交给 TUI worker
async def _cmd_goal(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        _warn(app, "cmd.core_disconnected")
        return
    argument = content.removeprefix("/goal").strip()
    action = "status"
    value = ""
    create_params: dict[str, object] | None = None
    draft = content
    if argument:
        head, _, tail = argument.partition(" ")
        if head in {
            "create",
            "status",
            "list",
            "pause",
            "resume",
            "edit",
            "complete",
            "clear",
            "cancel",
        }:
            action = head
            value = tail.strip()
        else:
            action = "create"
            value = argument
    if action == "create":
        try:
            value, create_params = _parse_goal_create_args(
                value,
                app._session_id,
                locale=_locale(app),
            )
        except ValueError as exc:
            detail = tr("cmd.goal.invalid", _locale(app), error=escape(str(exc)))
            usage = (
                "/goal create [--auto-continue|--no-auto-continue] "
                "[--max-auto-turns N] [--max-wall-seconds N] "
                "[--token-budget N] [--criterion \"criterion\"] "
                "[--constraint \"constraint\"] -- <goal>"
            )
            _warn(app, "cmd.usage", usage=f"{detail}\n{usage}")
            return
    if action == "edit" and not value:
        _warn(app, "cmd.usage", usage="/goal edit <new-goal>")
        return
    if action in {"status", "list", "pause", "resume"} and value:
        _warn(app, "cmd.usage", usage=f"/goal {action}")
        return
    if action in {"clear", "cancel"}:
        if value != "--yes":
            _warn(app, "cmd.goal.clear_confirm", action=action)
            return
        action = "clear"
        value = ""
    if action in {"create", "resume"} and app._busy:
        _warn(app, "cmd.goal.busy")
        return
    if action in {"create", "resume"}:
        app._begin_goal_command(
            ta,
            action,
            value,
            command_params=create_params,
            draft=draft,
        )
        return
    app.run_worker(
        app._do_goal_command(action, value, draft=draft),
        name=f"goal_{action}",
        exclusive=False,
    )


async def _cmd_mode(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if content == "/mode":
        current = tr(
            "cmd.mode.current",
            _locale(app),
            mode=app._input_runtime_mode.value,
        )
        app._append(
            Static(
                f"[bold cyan]{current}[/bold cyan]",
                classes="log-line",
            )
        )
        return
    raw_mode = content.removeprefix("/mode ").strip()
    try:
        mode = RuntimeMode(raw_mode)
    except ValueError:
        _warn(app, "cmd.usage", usage="/mode plan|act|operate")
    else:
        await app._select_runtime_mode(mode)


async def _cmd_permissions(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if content == "/permissions":
        ta.disabled = True
        _progress(app, ta, "cmd.permissions.select")
        try:
            app.query_one(PermissionModePicker).remove()
        except NoMatches:
            pass
        app.mount(
            PermissionModePicker(app._authority_preset, locale=_locale(app)),
            before="#prompt",
        )
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
        _warn(app, "cmd.usage", usage="/permissions ask|auto-review|full-access")
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
        _warn(app, "cmd.usage", usage="/trust status|grant|revoke")


async def _cmd_sandbox(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._show_sandbox_status()


# 解析 Worker start 的角色、route、模型、预算和只读/写入范围参数
def _parse_worker_start(argument: str, *, locale: str = "zh-CN") -> dict[str, object]:
    tokens = shlex.split(argument)
    profile = ""
    route_id = ""
    model = ""
    token_budget: int | None = None
    exact_files: list[str] = []
    write_roots: list[str] = []
    prompt_parts: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {
            "--profile",
            "--route",
            "--model",
            "--budget",
            "--file",
            "--write-root",
        }:
            if index + 1 >= len(tokens):
                raise ValueError(tr("cmd.worker.missing_arg", locale, option=token))
            value = tokens[index + 1]
            if token == "--profile":
                profile = value
            elif token == "--route":
                route_id = value
            elif token == "--model":
                model = value
            elif token == "--budget":
                if not value.isdigit() or int(value) < 1:
                    raise ValueError(tr("cmd.worker.budget_positive", locale))
                token_budget = int(value)
            elif token == "--file":
                exact_files.append(value)
            else:
                write_roots.append(value)
            index += 2
            continue
        if token.startswith("--"):
            raise ValueError(tr("cmd.worker.unknown_arg", locale, option=token))
        prompt_parts.append(token)
        index += 1
    prompt = " ".join(prompt_parts).strip()
    if not prompt:
        raise ValueError(tr("cmd.worker.empty_prompt", locale))
    return {
        "description": prompt[:120],
        "prompt": prompt,
        "profile": profile,
        "route_id": route_id,
        "model": model,
        "read_only": not (exact_files or write_roots),
        "exact_files": exact_files,
        "write_roots": write_roots,
        "token_budget": token_budget,
    }


# 解析 Worker 控制中心的启动、状态、重试、纠偏、取消、审查和显式应用动作
async def _cmd_workers(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        _warn(app, "cmd.core_disconnected")
        return
    argument = content.removeprefix("/workers").strip()
    if not argument:
        ta.disabled = True
        _progress(app, ta, "cmd.worker.loading")
        app.run_worker(app._show_workers(), name="view_workers", exclusive=False)
        return
    action, _, rest = argument.partition(" ")
    if action == "start" and rest:
        try:
            request = _parse_worker_start(rest, locale=_locale(app))
        except ValueError as exc:
            _warn(app, "cmd.worker.invalid", error=escape(str(exc)))
            return
        ta.disabled = True
        _progress(app, ta, "cmd.worker.starting")
        app.run_worker(
            app._do_worker_start(**request),
            name="start_worker",
            exclusive=False,
        )
        return
    if action == "status" and rest:
        worker_id = rest.strip()
        ta.disabled = True
        _progress(app, ta, "cmd.worker.status")
        app.run_worker(
            app._show_worker_status(worker_id),
            name="status_worker",
            exclusive=False,
        )
        return
    if action == "retry" and rest:
        worker_id = rest.replace("--yes", "").strip()
        if "--yes" not in rest.split():
            _warn(app, "cmd.worker.retry_confirm", worker=escape(worker_id))
            return
        ta.disabled = True
        _progress(app, ta, "cmd.worker.retrying")
        app.run_worker(
            app._do_worker_retry(worker_id),
            name="retry_worker",
            exclusive=False,
        )
        return
    if action == "peek" and rest:
        worker_id, _, cursor_text = rest.partition(" ")
        cursor = int(cursor_text) if cursor_text.isdigit() else 0
        ta.disabled = True
        _progress(app, ta, "cmd.worker.events")
        app.run_worker(
            app._show_worker_events(worker_id, cursor),
            name="peek_worker",
            exclusive=False,
        )
        return
    if action == "followup" and rest:
        worker_id, separator, message = rest.partition(" ")
        if separator and message.strip():
            ta.disabled = True
            _progress(app, ta, "cmd.worker.followup")
            app.run_worker(
                app._do_worker_followup(worker_id, message.strip()),
                name="followup_worker",
                exclusive=False,
            )
            return
    if action == "cancel" and rest:
        worker_id = rest.replace("--yes", "").strip()
        if "--yes" not in argument:
            _warn(app, "cmd.worker.cancel_confirm", worker=escape(worker_id))
            return
        ta.disabled = True
        _progress(app, ta, "cmd.worker.cancelling")
        app.run_worker(
            app._do_job_cancel(worker_id),
            name="cancel_worker",
            exclusive=False,
        )
        return
    if action == "review" and rest:
        parts = rest.split()
        if len(parts) == 1:
            ta.disabled = True
            _progress(app, ta, "cmd.worker.reviewing")
            app.run_worker(
                app._do_worker_review(parts[0], True, confirmed=False),
                name="review_worker",
                exclusive=False,
            )
            return
        if len(parts) >= 2 and parts[1] in {"approve", "reject"}:
            worker_id = parts[0]
            approved = parts[1] == "approve"
            digest = parts[2] if approved and len(parts) >= 3 else ""
            digest_valid = len(digest) == 64 and all(
                character in "0123456789abcdef" for character in digest
            )
            if approved and not digest_valid:
                _warn(app, "cmd.worker.digest_required")
                return
            if "--yes" not in parts:
                _warn(
                    app,
                    "cmd.worker.review_confirm",
                    worker=escape(worker_id),
                    decision=parts[1]
                    + (f" {escape(digest)}" if digest else ""),
                )
                return
            ta.disabled = True
            _progress(app, ta, "cmd.worker.review_recording")
            app.run_worker(
                app._do_worker_review(
                    worker_id,
                    approved,
                    confirmed=True,
                    expected_digest=digest,
                ),
                name="review_worker",
                exclusive=False,
            )
            return
    if action == "apply" and rest:
        parts = rest.split()
        if len(parts) in {2, 3}:
            worker_id, digest = parts[:2]
            digest_valid = len(digest) == 64 and all(
                character in "0123456789abcdef" for character in digest
            )
            if not digest_valid:
                _warn(app, "cmd.worker.digest_invalid")
                return
            if len(parts) == 2:
                _warn(
                    app,
                    "cmd.worker.apply_confirm",
                    worker=escape(worker_id),
                    digest=escape(digest),
                )
                return
            if parts[2] == "--yes":
                ta.disabled = True
                _progress(app, ta, "cmd.worker.applying")
                app.run_worker(
                    app._do_worker_apply(worker_id, digest),
                    name="apply_worker",
                    exclusive=False,
                )
                return
    _warn(app, "cmd.worker.usage")


def _make_viewer(
    name: str, target: str, needs_session: bool
) -> Callable[[Any, ChatTextArea, str], Awaitable[None]]:
    """为 tasks/workers/diff/rewind/context 生成共享的只读查看器 handler。"""

    async def handler(app: Any, ta: ChatTextArea, content: str) -> None:
        ta.text = ""
        if app._client is None or (needs_session and app._session_id is None):
            _warn(app, "cmd.core_disconnected")
            return
        ta.disabled = True
        _progress(app, ta, "cmd.loading", target=content)
        app.run_worker(getattr(app, target)(), name=f"view_{name}", exclusive=False)

    return handler


async def _cmd_workflow(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        _warn(app, "cmd.core_disconnected")
        return
    ta.disabled = True
    _progress(app, ta, "cmd.workflow.loading", command=content)
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
        _warn(app, "cmd.core_disconnected")
        return
    ta.disabled = True
    _progress(app, ta, "cmd.loading", target=content)
    turn_id = content.removeprefix("/turn").strip()
    app.run_worker(app._show_turn(turn_id), name="view_turn", exclusive=False)


async def _cmd_cost(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None:
        _warn(app, "cmd.core_disconnected")
        return
    ta.disabled = True
    _progress(app, ta, "cmd.loading", target="usage")
    app.run_worker(
        app._show_durable_cost_breakdown(),
        name="view_cost",
        exclusive=False,
    )


async def _cmd_skills(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    app._handle_skills_command(content)


async def _cmd_mcp(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        _warn(app, "cmd.core_disconnected")
        return
    ta.disabled = True
    _progress(app, ta, "cmd.loading", target=content)
    server = content.removeprefix("/mcp").strip()
    app.run_worker(app._show_mcp(server), name="view_mcp", exclusive=False)


async def _cmd_hooks(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        _warn(app, "cmd.core_disconnected")
        return
    if content == "/hooks" or content.startswith("/hooks "):
        arg = content.removeprefix("/hooks").strip()
        if arg.startswith("rerun "):
            hook_id = arg.removeprefix("rerun ").strip()
            if not hook_id:
                _warn(app, "cmd.hooks.rerun_usage")
                return
            if "--yes" not in content:
                _warn(app, "cmd.hooks.rerun_confirm", id=escape(hook_id))
                return
            ta.disabled = True
            _progress(app, ta, "cmd.hooks.rerunning")
            app.run_worker(
                app._do_hook_rerun(hook_id), name="rerun_hook", exclusive=False
            )
            return
        if arg:
            _warn(app, "cmd.hooks.usage")
            return
    ta.disabled = True
    _progress(app, ta, "cmd.hooks.loading")
    app.run_worker(app._show_hooks(), name="view_hooks", exclusive=False)


_MEMORY_USAGE = (
    "/memory | add <name> :: <body> | edit <id> :: <body> | "
    "pin|unpin <id> | expire <id> <ISO|never> | auto prompt|off | "
    "delete <id> --yes"
)


# 按双冒号拆分记忆名称或 ID 与允许包含空格的正文
def _split_memory_payload(value: str) -> tuple[str, str] | None:
    if "::" not in value:
        return None
    left, right = (part.strip() for part in value.split("::", 1))
    if not left or not right:
        return None
    return left, right


# 分发项目记忆的查看、治理和自动保存策略命令
async def _cmd_memory(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        _warn(app, "cmd.core_disconnected")
        return
    if content != "/memory" and content.startswith("/memory "):
        arg = content.removeprefix("/memory ").strip()
        if arg.startswith("delete "):
            delete_arg = arg.removeprefix("delete ").strip()
            confirmed = delete_arg.endswith(" --yes")
            memory_id = delete_arg.removesuffix(" --yes").strip()
            if not memory_id:
                _warn(app, "cmd.usage", usage="/memory delete <memory_id> --yes")
                return
            if not confirmed:
                _warn(app, "cmd.memory.delete_confirm", id=escape(memory_id))
                return
            ta.disabled = True
            _progress(app, ta, "cmd.memory.deleting")
            app.run_worker(
                app._do_memory_delete(memory_id), name="delete_memory", exclusive=False
            )
            return
        parts = arg.split(maxsplit=1)
        action, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        params: dict[str, object] | None = None
        if action in {"add", "edit"}:
            payload = _split_memory_payload(rest)
            if payload is not None:
                left, body = payload
                params = (
                    {"name": left, "body": body}
                    if action == "add"
                    else {"memory_id": left, "body": body}
                )
        elif action in {"pin", "unpin"} and rest:
            action = "pin"
            params = {"memory_id": rest, "pinned": parts[0] == "pin"}
        elif action == "expire" and rest:
            expire_parts = rest.split(maxsplit=1)
            if len(expire_parts) == 2:
                memory_id, value = expire_parts
                params = {
                    "memory_id": memory_id,
                    "expires_at": None if value in {"never", "clear"} else value,
                }
        elif action == "auto" and rest in {"prompt", "off"}:
            params = {"auto_save": rest}
        if params is not None:
            ta.disabled = True
            _progress(app, ta, "cmd.memory.running", action=action)
            app.run_worker(
                app._do_memory_action(action, params),
                name=f"memory_{action}",
                exclusive=False,
            )
            return
        _warn(app, "cmd.usage", usage=escape(_MEMORY_USAGE))
        return
    ta.disabled = True
    _progress(
        app,
        ta,
        "cmd.loading",
        target=tr("manage.memory.title", _locale(app)),
    )
    app.run_worker(app._show_memory(), name="view_memory", exclusive=False)


async def _cmd_jobs(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        _warn(app, "cmd.core_disconnected")
        return
    arg = content.removeprefix("/jobs").strip()
    if arg == "":
        ta.disabled = True
        _progress(app, ta, "cmd.jobs.loading")
        app.run_worker(app._show_jobs(), name="view_jobs", exclusive=False)
        return
    parts = arg.split(maxsplit=1)
    action, rest = parts[0], (parts[1] if len(parts) > 1 else "")
    if action == "show" and rest:
        ta.disabled = True
        _progress(app, ta, "cmd.jobs.output")
        app.run_worker(
            app._show_jobs(rest), name="show_job", exclusive=False
        )
        return
    if action == "cancel" and rest:
        if "--yes" not in content:
            _warn(app, "cmd.jobs.cancel_confirm", id=escape(rest))
            return
        ta.disabled = True
        _progress(app, ta, "cmd.jobs.cancelling")
        app.run_worker(
            app._do_job_cancel(rest), name="cancel_job", exclusive=False
        )
        return
    _warn(app, "cmd.jobs.usage")


# 解析 Artifact 清单/GC 命令并把副作用限定在显式 --yes 分支
async def _cmd_artifacts(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None:
        _warn(app, "cmd.core_disconnected")
        return
    parts = content.removeprefix("/artifacts").strip().split()
    if not parts:
        days = 30
        ta.disabled = True
        _progress(
            app,
            ta,
            "cmd.loading",
            target=tr("manage.artifacts.title", _locale(app)),
        )
        app.run_worker(app._show_artifacts(days), name="view_artifacts", exclusive=False)
        return
    if parts[0] != "gc":
        _warn(app, "cmd.usage", usage="/artifacts [gc [days] [--yes]]")
        return
    numeric = next((part for part in parts[1:] if part.isdigit()), "30")
    days = int(numeric)
    if days > 3650:
        _warn(app, "cmd.artifacts.days")
        return
    confirmed = "--yes" in parts
    ta.disabled = True
    _progress(app, ta, "cmd.artifacts.gc")
    app.run_worker(
        app._gc_artifacts(days, confirmed=confirmed),
        name="gc_artifacts",
        exclusive=False,
    )


# 解析显式确认的文件选择并启动 Change Center stage 动作
async def _cmd_stage(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None or app._busy:
        _warn(app, "cmd.stage.busy")
        return
    try:
        parts = shlex.split(content.removeprefix("/stage").strip())
    except ValueError:
        parts = []
    confirmed = "--yes" in parts
    paths = [part for part in parts if part != "--yes"]
    if not paths or not confirmed:
        selected = escape(
            ", ".join(paths) or tr("cmd.stage.placeholder", _locale(app))
        )
        _warn(app, "cmd.stage.confirm", paths=selected)
        return
    ta.disabled = True
    _progress(app, ta, "cmd.stage.running")
    app.run_worker(app._stage_changes(paths), name="stage_changes", exclusive=False)


# 解析显式确认的提交主题并启动只创建本地 commit 的动作
async def _cmd_commit(app: Any, ta: ChatTextArea, content: str) -> None:
    ta.text = ""
    if app._client is None or app._session_id is None or app._busy:
        _warn(app, "cmd.commit.busy")
        return
    raw = content.removeprefix("/commit").strip()
    confirmed = raw.endswith(" --yes") or raw == "--yes"
    message = raw.removesuffix(" --yes").strip() if confirmed else raw
    if not message or not confirmed:
        preview = escape(message or tr("cmd.commit.placeholder", _locale(app)))
        _warn(app, "cmd.commit.confirm", subject=preview)
        return
    ta.disabled = True
    _progress(app, ta, "cmd.commit.running")
    app.run_worker(app._commit_changes(message), name="commit_changes", exclusive=False)


# 内建命令的唯一事实来源；顺序即补全弹窗展示顺序
BUILTIN_SLASH_COMMANDS: list[SlashCommand] = [
    SlashCommand("help", "显示键位与全部命令", False, _cmd_help),
    SlashCommand("sessions", "打开会话选择器（输入即过滤）", True, _cmd_sessions),
    SlashCommand("new", "新建会话", True, _cmd_new),
    SlashCommand("rename", "重命名当前会话：/rename <标题>", True, _cmd_rename),
    SlashCommand("fork", "复制当前会话为分支：/fork [标题]", True, _cmd_fork),
    SlashCommand(
        "export",
        "导出当前会话：/export [md|json]",
        True,
        _cmd_export,
        usage="md|json",
        arg_candidates=("md", "json"),
    ),
    SlashCommand("delete", "删除当前会话（需 --yes 确认）", True, _cmd_delete),
    SlashCommand(
        "provider", "查看或切换 Provider route", False, _cmd_provider, usage="<route>"
    ),
    SlashCommand(
        "model",
        "查看或切换模型",
        False,
        _cmd_model,
        usage="<模型 ID>|add <模型 ID>",
    ),
    SlashCommand("doctor", "诊断活动 Provider route", False, _cmd_doctor),
    SlashCommand("config", "更换 LLM API、模型或密钥", False, _cmd_config),
    SlashCommand("compact", "手动压缩上下文", True, _cmd_compact),
    SlashCommand("copy", "复制上一条回复", False, _cmd_copy),
    SlashCommand(
        "history",
        "当前工作区输入历史：查看、开关或清空",
        False,
        _cmd_history,
        usage="status|on|off|clear",
        arg_candidates=("status", "on", "off", "clear"),
    ),
    SlashCommand(
        "language",
        "切换界面语言：中文或 English",
        False,
        _cmd_language,
        usage="zh-CN|en-US",
        arg_candidates=("zh-CN", "en-US"),
    ),
    SlashCommand(
        "attachments",
        "查看或移除待发送图片",
        False,
        _cmd_attachments,
        usage="remove <序号>|clear",
        arg_candidates=("remove", "clear"),
    ),
    SlashCommand("plan", "只读规划并审阅后再实施：/plan [任务]", False, _cmd_plan),
    SlashCommand("review", "只读复审当前改动：/review [关注点]", True, _cmd_review),
    SlashCommand(
        "goal",
        "持续执行并管理持久目标",
        True,
        _cmd_goal,
        usage=(
            "create [边界参数] <目标>|status|list|pause|resume|edit|complete|"
            "cancel --yes"
        ),
        arg_candidates=(
            "create",
            "status",
            "list",
            "pause",
            "resume",
            "edit",
            "complete",
            "cancel",
        ),
    ),
    SlashCommand(
        "mode",
        "查看或切换工作模式：plan|act|operate",
        False,
        _cmd_mode,
        usage="plan|act|operate",
        arg_candidates=("plan", "act", "operate"),
    ),
    SlashCommand(
        "permissions",
        "查看或切换权限模式",
        False,
        _cmd_permissions,
        usage="ask|auto-review|full-access",
        arg_candidates=("ask", "auto-review", "full-access"),
    ),
    SlashCommand(
        "trust",
        "查看或授予/撤销工作区信任",
        False,
        _cmd_trust,
        usage="status|grant|revoke",
        arg_candidates=("status", "grant", "revoke"),
    ),
    SlashCommand("sandbox", "查看 OS 隔离能力（仅探测）", False, _cmd_sandbox),
    SlashCommand(
        "tasks", "查看最近一次 run 的任务", True, _make_viewer("tasks", "_show_tasks", True)
    ),
    SlashCommand(
        "workers",
        "查看、审查或应用持久 Worker",
        True,
        _cmd_workers,
        usage="start|status|retry|peek|followup|cancel|review|apply",
        arg_candidates=(
            "start",
            "status",
            "retry",
            "peek",
            "followup",
            "cancel",
            "review",
            "apply",
        ),
    ),
    SlashCommand(
        "workflow",
        "查看、启动或检查 workflow",
        True,
        _cmd_workflow,
        usage="list|get|start <path>",
        arg_candidates=("list", "get", "start"),
        labs=True,
    ),
    SlashCommand(
        "changes",
        "打开可导航的改动中心",
        True,
        _make_viewer("changes", "_show_changes", True),
    ),
    SlashCommand(
        "diff", "查看工作区改动", True, _make_viewer("diff", "_show_diff", True)
    ),
    SlashCommand(
        "stage",
        "选择文件加入 Git index（需 --yes）",
        True,
        _cmd_stage,
        usage="<path...> --yes",
    ),
    SlashCommand(
        "commit",
        "从已 stage 改动创建本地 commit（需 --yes）",
        True,
        _cmd_commit,
        usage="<提交主题> --yes",
    ),
    SlashCommand(
        "rewind",
        "预览并二次确认安全恢复点",
        True,
        _cmd_rewind,
        usage="[--yes]",
        arg_candidates=("--yes",),
    ),
    SlashCommand(
        "context",
        "查看上下文占用与用量",
        True,
        _make_viewer("context", "_show_context", True),
    ),
    SlashCommand("cost", "查看本会话成本分解与缓存节省", False, _cmd_cost),
    SlashCommand("turn", "检查 route、用量、审批与收据", True, _cmd_turn, usage="<turn_id>"),
    SlashCommand(
        "skills",
        "列出、查看、安装或删除 skills",
        False,
        _cmd_skills,
        usage="list|show|install|remove|audit",
        arg_candidates=("list", "show", "install", "remove", "audit"),
    ),
    SlashCommand("mcp", "查看 MCP server 状态与工具", True, _cmd_mcp),
    SlashCommand(
        "hooks",
        "查看 hook 配置与执行记录",
        True,
        _cmd_hooks,
        usage="rerun <id>",
        arg_candidates=("rerun",),
        labs=True,
    ),
    SlashCommand(
        "memory",
        "查看、编辑并控制项目记忆",
        True,
        _cmd_memory,
        usage="add|edit|pin|unpin|expire|auto|delete",
        arg_candidates=("add", "edit", "pin", "unpin", "expire", "auto", "delete"),
    ),
    SlashCommand(
        "jobs",
        "后台任务中心：查看/取消",
        True,
        _cmd_jobs,
        usage="show <id>",
        arg_candidates=("show", "cancel"),
    ),
    SlashCommand(
        "artifacts",
        "查看产物或执行引用感知 GC",
        True,
        _cmd_artifacts,
        usage="gc [days] [--yes]",
        arg_candidates=("gc",),
    ),
]


# 返回面向命令面板的稳定命令，Labs 未显式启用时隐藏实验项
def visible_slash_commands(*, labs_enabled: bool = False) -> list[SlashCommand]:
    return [
        command
        for command in BUILTIN_SLASH_COMMANDS
        if labs_enabled or not command.labs
    ]


def match_slash_command(content: str) -> SlashCommand | None:
    """按 `/name` 或 `/name <args>` 精确匹配单个命令；未知前缀返回 None。"""
    for cmd in BUILTIN_SLASH_COMMANDS:
        if content == f"/{cmd.name}" or content.startswith(f"/{cmd.name} "):
            return cmd
    return None


# 解析 /命令 <参数> 形式并按 Tab 循环补全下一个参数候选；不适用时返回 None
def complete_command_arg_text(text: str) -> str | None:
    if not (text.startswith("/") and " " in text):
        return None
    cmd = match_slash_command(text)
    if cmd is None or not cmd.arg_candidates:
        return None
    candidates = cmd.arg_candidates
    cmd_prefix = f"/{cmd.name} "
    partial = text[len(cmd_prefix):]
    # 已精确命中某个候选时循环进位到下一候选，实现多次 Tab 环绕
    for i, cand in enumerate(candidates):
        if partial and partial.lower() == cand.lower():
            nxt = candidates[(i + 1) % len(candidates)]
            return f"{cmd_prefix}{nxt}"
    # 未精确命中则补全到首个前缀匹配的候选
    lowered = partial.lower()
    for cand in candidates:
        if cand.lower().startswith(lowered):
            return f"{cmd_prefix}{cand}"
    return None

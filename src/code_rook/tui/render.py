"""事件渲染器：把 App 的事件分支按事件族组织为渲染函数。

阶段 5 的目标是把 ``app.py`` 的 ``_handle_event_inner`` 里约 30 个事件分支按事件族
迁出。所有渲染副作用都通过参数传入的 ``app``（类型为 ``Any``，避免循环导入）触发，
App 只保留顶层 try/except 保护与编排，交互语义、渲染文案与消息泵交互完全不变。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from rich.markup import escape
from textual.css.query import NoMatches
from textual.widgets import Static

from code_rook.tui.product import tr
from code_rook.tui.widgets import _preview as _preview
from code_rook.tui.widgets.permission import PermissionBlock, PermissionSelect
from code_rook.tui.widgets.pickers import PlanReview, UserQuestionSelect
from code_rook.tui.widgets.stream import LLMStreamBlock, ToolCallBlock, ToolStepGroup

log = logging.getLogger(__name__)


# 读取宿主界面语言，旧测试替身缺少字段时保持中文默认
def _locale(app: Any) -> str:
    return str(getattr(app, "_locale", "zh-CN"))

# 事件渲染总入口：先处理 LLM 流式前段，统一换块，再按事件族分派
def render_event(app: Any, event: dict[str, Any]) -> None:
    if _render_llm_front(app, event):
        return
    app._break_llm()
    _render_rest(app, event)


# 处理 LLM 流式前段事件（命中则返回 True，调用方跳过 break_llm 换块）
def _render_llm_front(app: Any, event: dict[str, Any]) -> bool:
    t = event.get("type", "")
    if t == "llm.reasoning":
        content = str(event.get("content") or "")
        app._break_llm()
        if content.strip():
            reasoning_block = LLMStreamBlock(locale=_locale(app))
            reasoning_block.append_token(content)
            reasoning_block.set_kind("reasoning")
            reasoning_block.finalize_markdown()
            app._append(reasoning_block)
        return True

    if t == "llm.token":
        token = event.get("token", "")
        if app._current_llm is None:
            llm_block = LLMStreamBlock(locale=_locale(app))
            app._append(llm_block)
            app._current_llm = llm_block
        app._current_llm.append_token(token)
        return True

    if t == "agent.decision":
        intent = str(event.get("intent") or "execute")
        if app._current_llm is not None:
            app._current_llm.set_kind("answer" if intent == "respond" else intent)
        app._break_llm()
        return True

    if t == "llm.usage":
        run_id = event.get("run_id", "")
        if run_id in app._subagent_run_ids:
            return True
        pct = float(event.get("context_pct") or 0.0)
        app._last_context_pct = pct
        app._accumulate_cost(event)
        app._update_header(app._header_state)
        return True

    return False


# 处理 LLM 尾部与 agent 类事件，渲染顶栏与重试/卡住的提示行
def _render_llm_tail(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "llm.route_selected":
        app._route = str(event.get("route_id") or "")
        app._model = str(event.get("model") or "")
        app._update_header("running")

    elif t == "llm.retry":
        kind = str(event.get("kind") or "retry")
        attempt = int(event.get("attempt") or 0)
        retry = tr(
            "event.llm.retry",
            _locale(app),
            kind=escape(kind),
            attempt=attempt,
        )
        app._append(
            Static(
                f"[dim]{retry}[/dim]",
                classes="log-line",
            )
        )

    elif t == "agent.stuck":
        tool_name = escape(str(event.get("tool_name") or "tool"))
        repeat_count = int(event.get("repeat_count") or 0)
        stopped = tr(
            "event.agent.stuck",
            _locale(app),
            count=repeat_count,
        )
        app._append(
            Static(
                f"[yellow]{stopped}[/yellow]  [bold]{tool_name}[/bold]",
                classes="log-line",
            )
        )


# 处理会话生命周期事件：等待输入、被中断、已关闭
def _render_session(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "session.waiting_for_input":
        app._busy = False
        app._cancel_requested = False
        app._cancel_armed = False
        app._clear_user_question()
        prompt = app._prompt()
        if prompt is not None:
            prompt.disabled = app._plan_review_pending
            prompt.read_only = False
            if app._plan_review_pending:
                prompt.border_title = tr("shell.review_plan", _locale(app))
            else:
                prompt.border_title = tr("shell.connected", _locale(app))
                prompt.focus()
        app._update_header("plan ready" if app._plan_review_pending else "ready")
        call_later = getattr(app, "call_later", None)
        flush_queue = getattr(app, "_flush_queued_message", None)
        if callable(call_later) and callable(flush_queue):
            call_later(flush_queue)

    elif t == "session.interrupted":
        app._busy = False
        app._active_run_id = None
        app._cancel_requested = False
        app._cancel_armed = False
        app._clear_user_question()
        prompt = app._prompt()
        if prompt is not None:
            prompt.disabled = False
            prompt.read_only = False
            prompt.border_title = tr("shell.cancelled", _locale(app))
            prompt.focus()
        app._update_header("interrupted")

    elif t == "session.closed":
        app._busy = False
        app._cancel_requested = False
        app._clear_user_question()
        prompt = app._prompt()
        if prompt is not None:
            prompt.disabled = True
            prompt.read_only = False
            prompt.border_title = tr("shell.disconnected", _locale(app))
        app._update_header("disconnected")


# 处理计划事件：挂载计划审阅面板或追加更新日志
def _render_plan(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "plan.ready":
        run_id = str(event.get("run_id", ""))
        session_id = str(event.get("session_id", ""))
        if session_id != app._session_id:
            return
        app._plan_review_pending = True
        app._plan_session_id = session_id
        app._plan_run_id = run_id
        app._plan_request = str(event.get("request", ""))
        try:
            app.query_one(PlanReview).remove()
        except NoMatches:
            pass
        prompt = app._prompt()
        if prompt is not None:
            prompt.disabled = True
            prompt.border_title = tr("shell.review_plan", _locale(app))
        app.mount(PlanReview(run_id, locale=_locale(app)), before="#prompt")
        app._update_header("plan ready")

    elif t == "plan.resolved":
        session_id = str(event.get("session_id", ""))
        run_id = str(event.get("run_id", ""))
        if (
            session_id != app._session_id
            or session_id != app._plan_session_id
            or run_id != app._plan_run_id
        ):
            return
        app._clear_plan_review()
        if not app._busy:
            app._restore_ready_prompt()
            app._update_header("ready")

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
        app._append(Static("\n".join(lines), classes="log-line"))


# 渲染有限 Goal Loop 的继续或暂停决策及累计预算
def _render_goal(app: Any, event: dict[str, Any]) -> None:
    should_continue = bool(event.get("should_continue", False))
    reason = escape(str(event.get("reason") or "unknown"))
    used = max(0, int(event.get("auto_turns_used", 0) or 0))
    remaining = max(0, int(event.get("remaining_auto_turns", 0) or 0))
    total = used + remaining
    tokens_used = max(0, int(event.get("tokens_used", 0) or 0))
    token_budget = event.get("token_budget")
    token_limit = escape(str(token_budget)) if token_budget is not None else "unbounded"
    wall_elapsed = max(0, int(event.get("wall_elapsed_seconds", 0) or 0))
    max_wall = max(1, int(event.get("max_wall_seconds", 1) or 1))
    confirmation = bool(event.get("paused_needs_confirmation", False))
    if should_continue:
        title = f"[bold green]{tr('event.goal.continue', _locale(app))}[/bold green]"
    elif confirmation:
        title = f"[bold yellow]{tr('event.goal.paused', _locale(app))}[/bold yellow]"
    else:
        title = f"[bold cyan]{tr('event.goal.ended', _locale(app))}[/bold cyan]"
    next_action = (
        f"\n[yellow]{tr('event.goal.resume', _locale(app))}[/yellow]"
        if confirmation
        else ""
    )
    app._append(
        Static(
            f"{title}  [dim]{reason}[/dim]\n"
            f"[dim]auto={used}/{total}  tokens={tokens_used}/{token_limit}  "
            f"wall={wall_elapsed}s/{max_wall}s[/dim]{next_action}",
            classes="log-line",
        )
    )


# 处理结构化用户问题，挂载问题选择面板
def _render_user_question(app: Any, event: dict[str, Any]) -> None:
    session_id = str(event.get("session_id", ""))
    if session_id != app._session_id:
        return
    question_id = str(event.get("question_id", ""))
    app._pending_question_id = question_id
    app._answering_question = False
    try:
        app.query_one(UserQuestionSelect).remove()
    except NoMatches:
        pass
    prompt = app._prompt()
    if prompt is not None:
        prompt.disabled = True
        prompt.border_title = tr("question.prompt", _locale(app))
    app.mount(
        UserQuestionSelect(
            question_id,
            str(event.get("question", "")),
            str(event.get("header", "Question")),
            [str(option) for option in event.get("options", [])],
            bool(event.get("multi_select", False)),
            locale=_locale(app),
        ),
        before="#prompt",
    )
    app._update_header("question")


# 处理 run 生命周期事件：开始记录状态、结束清理并渲染结果
def _render_run(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "run.phase_changed":
        phase = str(event.get("phase") or "running")
        previous = getattr(app, "_run_phase", "")
        app._run_phase = phase
        app._run_phase_current = int(event.get("current") or 0)
        app._run_phase_total = int(event.get("total") or 0)
        app._update_header(phase)
        if phase != previous and phase not in {"completed", "failed", "interrupted"}:
            labels = {
                "zh-CN": {
                    "understanding": "理解任务",
                    "exploring": "定位问题",
                    "planning": "制定计划",
                    "waiting_confirmation": "等待确认",
                    "executing": "修改与执行",
                    "verifying": "验证结果",
                    "reviewing": "审查变更",
                },
                "en-US": {
                    "understanding": "Understand task",
                    "exploring": "Locate problem",
                    "planning": "Build plan",
                    "waiting_confirmation": "Wait for approval",
                    "executing": "Edit and execute",
                    "verifying": "Verify result",
                    "reviewing": "Review changes",
                },
            }
            locale = "en-US" if _locale(app) == "en-US" else "zh-CN"
            label = labels[locale].get(phase, phase)
            summary = escape(str(event.get("summary") or ""))
            detail = f"  [dim]{summary}[/dim]" if summary else ""
            app._append(
                Static(
                    f"[bold cyan]● {label}[/bold cyan]{detail}",
                    classes="log-line",
                )
            )

    elif t == "run.started":
        run_id = str(event.get("run_id", ""))
        app._active_run_id = run_id
        app._current_steps.pop(run_id, None)
        app._cancel_requested = False
        app._cancel_armed = False

    elif t == "run.finished":
        status = event.get("status", "")
        steps = event.get("steps", 0)
        step_label = "step" if steps == 1 else "steps"
        reason = str(event.get("reason") or "")
        run_id = str(event.get("run_id", ""))
        app._current_steps.pop(run_id, None)
        for group_key in [
            key for key in app._tool_step_groups if key[0] == run_id
        ]:
            app._tool_step_groups.pop(group_key, None)
        app._active_run_id = None
        app._cancel_requested = False
        app._cancel_armed = False
        schedule_result = getattr(app, "_schedule_run_result", None)
        scheduled = bool(schedule_result(event)) if callable(schedule_result) else False
        if status == "success":
            app._maybe_autotitle_session()
            return
        if scheduled:
            return
        if reason == "cancelled":
            cancelled = tr(
                "event.run.cancelled",
                _locale(app),
                steps=steps,
                unit=step_label,
            )
            app._append(Static(
                f"[yellow]–[/yellow] [dim]{cancelled}[/dim]",
                classes="run-err",
            ))
        else:
            detail = f"  [dim]{escape(reason)}[/dim]" if reason else ""
            model_failure = reason == "llm_error" or reason.startswith("model_error")
            guidance = (
                f"\n[dim]{tr('event.run.model_guidance', _locale(app))}[/dim]"
                if model_failure
                else ""
            )
            failed = tr(
                "event.run.failed",
                _locale(app),
                steps=steps,
                unit=step_label,
            )
            app._append(Static(
                f"[red]×[/red] [dim]{failed}[/dim]"
                f"{detail}{guidance}",
                classes="run-err",
            ))


# 处理技能调用、子代理起止与后台任务的日志类事件
def _render_stage(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "skill.invoked":
        skill_name = event.get("skill_name", "")
        arguments = event.get("arguments", "")
        args_preview = _preview(arguments, 80) if arguments else ""
        args_part = f"  [dim]{args_preview}[/dim]" if args_preview else ""
        app._append(Static(
            f"[bold cyan]/{skill_name}[/bold cyan]{args_part}",
            classes="log-line",
        ))

    elif t == "subagent.started":
        run_id = event.get("run_id", "")
        description = event.get("description", "")
        app._subagent_run_ids[run_id] = description
        app._subagent_start_times[run_id] = time.monotonic()
        short_id = run_id[:8] if len(run_id) >= 8 else run_id
        active = len(app._subagent_run_ids)
        app._append(Static(
            f"[bold cyan]▣ {active} Agent{'s' if active != 1 else ''} working[/bold cyan]\n"
            f"  [dim]◌[/dim] [cyan]{_preview(description, 72)}[/cyan]  [dim]{short_id}[/dim]",
            classes="log-line",
        ))

    elif t == "subagent.finished":
        run_id = event.get("run_id", "")
        status = event.get("status", "")
        description = app._subagent_run_ids.pop(
            run_id, event.get("description", "")
        )
        start = app._subagent_start_times.pop(run_id, None)
        elapsed = (
            f"  [dim]{time.monotonic() - start:.1f}s[/dim]"
            if start is not None
            else ""
        )
        desc_part = f"[cyan]{_preview(description, 72)}[/cyan]{elapsed}"
        if status == "success":
            app._append(Static(
                f"  [bold green]✓[/bold green] {desc_part}",
                classes="log-line",
            ))
        else:
            app._append(Static(
                f"  [bold red]×[/bold red] {desc_part}",
                classes="log-line",
            ))

    elif t == "background.started":
        job_id = str(event.get("job_id", ""))
        command = _preview(str(event.get("command", "")), 76)
        app._append(
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
        app._append(
            Static(
                f"{marker} [cyan]{job_id}[/cyan]  [dim]{status}[/dim]",
                classes="log-line",
            )
        )

    elif t == "hook.executed":
        hook_id = str(event.get("hook_id", ""))
        status = str(event.get("status", ""))
        elapsed_ms = int(event.get("elapsed_ms") or 0)
        marker = "[bold green]hook[/bold green]" if status == "completed" else (
            "[bold red]hook[/bold red]"
        )
        detail = f"  [dim]{status} · {elapsed_ms}ms[/dim]"
        reason = str(event.get("reason") or "")
        if status in {"failed", "blocked", "timeout"} and reason:
            detail += f"  [dim]{escape(reason[:120])}[/dim]"
        app._append(
            Static(
                f"{marker} [cyan]{escape(hook_id)}[/cyan]{detail}",
                classes="log-line",
            )
        )


# 处理 step 起止事件，维护步号索引
def _render_step(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "step.started":
        run_id = str(event.get("run_id", ""))
        if run_id in app._subagent_run_ids:
            return
        app._current_steps[run_id] = int(event.get("step") or 0)

    elif t == "step.finished":
        run_id = str(event.get("run_id", ""))
        step = int(event.get("step") or app._current_steps.get(run_id, 0))
        app._tool_step_groups.pop((run_id, step), None)
        if app._current_steps.get(run_id) == step:
            app._current_steps.pop(run_id, None)


# 处理工具调用的开始/完成/失败，驱动步骤分组与结果回填
def _render_tool(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "tool.call_started":
        tool_use_id = str(event.get("tool_use_id", ""))
        tool_name = str(event.get("tool_name", ""))
        raw_params = event.get("params") or {}
        params = raw_params if isinstance(raw_params, dict) else {}
        raw_presentation = event.get("presentation") or {}
        presentation = raw_presentation if isinstance(raw_presentation, dict) else {}
        run_id = str(event.get("run_id", ""))
        tc_block = ToolCallBlock(
            tool_name,
            params,
            locale=_locale(app),
            presentation=presentation,
        )
        if run_id in app._subagent_run_ids:
            tc_block.styles.padding = (0, 2, 0, 6)
            app._append(tc_block)
        else:
            step = app._current_steps.get(run_id, 0)
            group_key = (run_id, step)
            group = app._tool_step_groups.get(group_key)
            if group is None:
                group = ToolStepGroup(step, locale=_locale(app))
                app._tool_step_groups[group_key] = group
                group.add_tool(tc_block)
                app._append(group)
            else:
                group.add_tool(tc_block)
        app._pending_tool_blocks[tool_use_id] = tc_block

    elif t == "tool.call_finished":
        tool_use_id = str(event.get("tool_use_id", ""))
        elapsed_ms = int(event.get("elapsed_ms") or 0)
        output = str(event.get("output") or "")
        raw_presentation = event.get("presentation") or {}
        presentation = raw_presentation if isinstance(raw_presentation, dict) else {}
        if tool_use_id in app._pending_tool_blocks:
            tc_done = app._pending_tool_blocks.pop(tool_use_id)
            tc_done.set_result(output, elapsed_ms, presentation=presentation)

    elif t == "tool.call_failed":
        tool_use_id = str(event.get("tool_use_id", ""))
        elapsed_ms = int(event.get("elapsed_ms") or 0)
        error_msg = str(event.get("error_message") or "")
        raw_presentation = event.get("presentation") or {}
        presentation = raw_presentation if isinstance(raw_presentation, dict) else {}
        if event.get("terminal") is False:
            return
        if tool_use_id in app._pending_tool_blocks:
            tc_done = app._pending_tool_blocks.pop(tool_use_id)
            tc_done.set_result(
                error_msg,
                elapsed_ms,
                is_error=True,
                presentation=presentation,
            )
        category = escape(str(event.get("failure_category") or event.get("error_class") or "tool"))
        action = (
            "检查权限或修改命令后重试"
            if _locale(app) == "zh-CN"
            else "Check permission or adjust the command, then retry"
        )
        app._append(
            Static(
                f"[yellow]{category}[/yellow]  [dim]{action}[/dim]",
                classes="log-line",
            )
        )


# 处理上下文压缩、权限审批、LSP 诊断与日志等杂项事件
def _render_misc(app: Any, t: str, event: dict[str, Any]) -> None:
    if t == "context.repository":
        repository_paths = [str(path) for path in event.get("paths", [])]
        used = int(event.get("used_chars", 0))
        budget = int(event.get("budget_chars", 0))
        cache_hits = int(event.get("cache_hits", 0))
        parsed = int(event.get("parsed_files", 0))
        preview = ", ".join(repository_paths[:4]) or "none"
        suffix = (
            f" +{len(repository_paths) - 4}" if len(repository_paths) > 4 else ""
        )
        app._append(
            Static(
                "[bold cyan]Repository context[/bold cyan]  "
                f"[dim]{len(repository_paths)} files · {used}/{budget} chars · "
                f"cache={cache_hits} parsed={parsed}[/dim]\n"
                f"[dim]  {escape(preview)}{suffix}[/dim]",
                classes="log-line",
            )
        )

    elif t == "context.working_set":
        working_paths = [str(path) for path in event.get("paths", [])]
        preview = ", ".join(working_paths[:6]) or "none"
        suffix = f" +{len(working_paths) - 6}" if len(working_paths) > 6 else ""
        app._append(
            Static(
                f"[bold cyan]Working set[/bold cyan]  [dim]{escape(preview)}{suffix}[/dim]",
                classes="log-line",
            )
        )

    elif t in {"verification.completed", "verification.failed"}:
        passed = int(event.get("passed", 0))
        failed = int(event.get("failed", 0))
        action = escape(str(event.get("action", "verify")))
        verification_paths = ", ".join(
            str(path) for path in event.get("paths", [])[:3]
        )
        raw_gates = event.get("gates", [])
        gates = raw_gates if isinstance(raw_gates, list) else []
        gate_names = ", ".join(
            escape(str(gate.get("name", "gate")))
            for gate in gates[:4]
            if isinstance(gate, dict)
        )
        if t == "verification.completed":
            headline = f"[bold green]Verification passed[/bold green]  {action}"
        else:
            failure = escape(str(event.get("failure_class", "verification_failed")))
            headline = (
                f"[bold red]Verification failed[/bold red]  {action}  "
                f"[dim]{failure}[/dim]"
            )
        detail = f"{passed} passed / {failed} failed"
        if gate_names:
            detail += f" · {gate_names}"
        if verification_paths:
            detail += f" · {escape(verification_paths)}"
        app._append(Static(f"{headline}\n[dim]  {detail}[/dim]", classes="log-line"))

    elif t == "context.compacted":
        orig = event.get("original_tokens", 0)
        compacted = event.get("compacted_tokens", 0)
        pinned = int(event.get("pinned_fact_retained", 0))
        if int(orig) > 0:
            app._last_context_pct *= int(compacted) / int(orig)
        else:
            app._last_context_pct = 0.0
        app._append(Static(
            f"[bold cyan]Context compacted[/bold cyan]  "
            f"[dim]{orig} → {compacted} tokens · retained {pinned} task facts[/dim]",
            classes="log-line",
        ))

    elif t == "recovery.available":
        safe = bool(event.get("safe_to_resume", False))
        marker = "[green]✓[/green]" if safe else "[yellow]![/yellow]"
        summary = escape(str(event.get("summary") or "Interrupted turn available"))
        actions = (
            "继续 · 查看变更 · 恢复 Checkpoint · 放弃本轮 · 导出诊断"
            if _locale(app) == "zh-CN"
            else "Continue · View changes · Rewind · Abandon turn · Export diagnostics"
        )
        app._append(
            Static(
                f"[bold cyan]Recovery[/bold cyan] {marker}\n{summary}\n[dim]{actions}[/dim]",
                classes="product-card",
            )
        )

    elif t == "permission.requested":
        tool_use_id = str(event.get("tool_use_id", ""))
        tool_name = str(event.get("tool_name", ""))
        param_preview = str(event.get("param_preview", ""))
        raw_params = event.get("params", {})
        params = raw_params if isinstance(raw_params, dict) else {}
        try:
            _focused_repr = repr(app.focused)
        except Exception:
            _focused_repr = "?"
        log.info(
            "permission.requested tool=%s id=%s  app.focused=%s",
            tool_name, tool_use_id, _focused_repr,
        )
        perm_block = PermissionBlock(
            tool_use_id,
            tool_name,
            param_preview,
            locale=_locale(app),
        )
        app._pending_permission_blocks[tool_use_id] = perm_block
        prompt = app._prompt()
        if prompt is not None:
            prompt.disabled = True
            prompt.border_title = tr("permission.pending", _locale(app))
        app._append(perm_block)
        select = PermissionSelect(
            tool_use_id,
            tool_name,
            param_preview,
            params,
            locale=_locale(app),
        )
        app._mount_permission_select(select)
        log.debug(
            "PermissionSelect mounted before #prompt  pending=%d",
            len(app._pending_permission_blocks),
        )

    elif t in {"permission.granted", "permission.denied"}:
        # 清理来自其他客户端或历史对账的已解决审批，拒绝时再显示可见提示。
        tool_use_id = str(event.get("tool_use_id", ""))
        if tool_use_id in app._pending_permission_blocks:
            perm_block = app._pending_permission_blocks.pop(tool_use_id)
            if t == "permission.denied":
                denied_tool = escape(perm_block._tool_name)
                denied = tr(
                    "event.permission.denied",
                    _locale(app),
                    tool=denied_tool,
                )
                app._append(
                    Static(
                        f"[yellow]{denied}[/yellow]",
                        classes="log-line",
                    )
                )
            perm_block.remove()
            try:
                select = app.query_one(PermissionSelect)
                select.remove()
            except Exception:
                pass
            if not app._pending_permission_blocks:
                p = app._prompt()
                if p is not None:
                    p.disabled = False
                    p.read_only = False
                    p.border_title = tr("shell.connected", _locale(app))
                    p.focus()

    elif t == "lsp.diagnostics":
        status = str(event.get("status", ""))
        tool = escape(str(event.get("tool", "")))
        count = int(event.get("diagnostic_count", 0))
        diagnostic_paths = ", ".join(str(p) for p in event.get("paths", [])[:3])
        if status == "ok" and count == 0:
            line = (
                f"[green]{tr('event.diagnostics.passed', _locale(app))}[/green]  "
                f"[dim]{tool} · {escape(diagnostic_paths)}[/dim]"
            )
        elif status == "ok":
            issues = tr(
                "event.diagnostics.issues",
                _locale(app),
                count=count,
            )
            line = (
                f"[yellow]{issues}[/yellow]  "
                f"[dim]{tool} · {escape(diagnostic_paths)}[/dim]"
            )
        else:
            error = escape(str(event.get("error", ""))[:120])
            degraded = tr(
                "event.diagnostics.degraded",
                _locale(app),
                status=status,
            )
            line = f"[dim]{degraded} · {tool} · {error}[/dim]"
        app._append(Static(line, classes="log-line"))

    elif t == "log.line":
        level = event.get("level", "INFO")
        color = (
            "bold red"
            if level == "ERROR"
            else ("yellow" if level == "WARNING" else "dim")
        )
        app._append(Static(
            f"[{color}]{level}[/{color}]  "
            f"[dim]{event.get('source', '')}[/dim]  {event.get('message', '')}",
            classes="log-line",
        ))


# 在 break_llm 之后按事件族分派剩余渲染分支
def _render_rest(app: Any, event: dict[str, Any]) -> None:
    t = event.get("type", "")
    if t.startswith("llm.") or t == "agent.stuck":
        _render_llm_tail(app, t, event)
    elif t.startswith("session."):
        _render_session(app, t, event)
    elif t.startswith("plan."):
        _render_plan(app, t, event)
    elif t == "goal.continue_decision":
        _render_goal(app, event)
    elif t == "user_question.asked":
        _render_user_question(app, event)
    elif t.startswith("run."):
        _render_run(app, t, event)
    elif t.startswith(("skill.", "subagent.", "background.", "hook.")):
        _render_stage(app, t, event)
    elif t.startswith("step."):
        _render_step(app, t, event)
    elif t.startswith("tool."):
        _render_tool(app, t, event)
    else:
        _render_misc(app, t, event)

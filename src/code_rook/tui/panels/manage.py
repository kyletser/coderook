"""管理面板四件套（/mcp /hooks /memory /jobs）的纯渲染函数。

所有函数都是无副作用的富文本字符串构造，输入取自 IPC 返回的 dict 数据，
供 app.py 的看板方法直接 ``Static(body)`` 展示，也与面板单测解耦。
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape

from code_rook.tui.product import tr

# 各面板共用的状态徽标映射
_STATUS_MARKERS = {
    "connected": "[green]●[/green]",
    "running": "[blue]▶[/blue]",
    "queued": "[cyan]○[/cyan]",
    "completed": "[green]✓[/green]",
    "success": "[green]✓[/green]",
    "failed": "[red]●[/red]",
    "cancelled": "[yellow]−[/yellow]",
    "interrupted": "[magenta]−[/magenta]",
    "blocked": "[red]×[/red]",
    "skipped_untrusted": "[dim]−[/dim]",
    "timeout": "[red]T[/red]",
    "dropped": "[dim]~[/dim]",
}


# 将字节数格式化为紧凑二进制单位
def _format_bytes(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


# 渲染 artifact 清单、引用状态与 GC 候选摘要
def render_artifacts(payload: dict[str, Any], *, locale: str = "zh-CN") -> str:
    artifacts = payload.get("artifacts", [])
    if not isinstance(artifacts, list):
        artifacts = []
    total = int(payload.get("total_bytes", 0))
    reclaimable = int(payload.get("reclaimable_bytes", 0))
    lines = [
        f"[bold cyan]{tr('manage.artifacts.title', locale)}[/bold cyan]",
        "[dim]"
        + tr(
            "manage.artifacts.summary",
            locale,
            count=len(artifacts),
            total=_format_bytes(total),
            reclaimable=_format_bytes(reclaimable),
        )
        + "[/dim]",
    ]
    for item in artifacts[:30]:
        if not isinstance(item, dict):
            continue
        sha = escape(str(item.get("sha256", ""))[:12])
        size = _format_bytes(int(item.get("size", 0)))
        referenced = bool(item.get("referenced", False))
        candidate = bool(item.get("gc_candidate", False))
        marker = (
            f"[green]{tr('manage.artifacts.kept', locale)}[/green]"
            if referenced
            else (
                f"[yellow]{tr('manage.artifacts.candidate', locale)}[/yellow]"
                if candidate
                else f"[dim]{tr('manage.artifacts.recent', locale)}[/dim]"
            )
        )
        lines.append(f"  {marker}  [cyan]{sha}[/cyan]  [dim]{size}[/dim]")
    if len(artifacts) > 30:
        lines.append(
            f"[dim]⋯ {tr('manage.artifacts.more', locale, count=len(artifacts) - 30)}[/dim]"
        )
    lines.append(f"[dim]{tr('manage.artifacts.hint', locale)}[/dim]")
    return "\n".join(lines)


# 渲染 Artifact GC 预览或已确认删除结果
def render_artifact_gc(payload: dict[str, Any], *, locale: str = "zh-CN") -> str:
    dry_run = bool(payload.get("dry_run", True))
    candidates = payload.get("candidates", [])
    removed = payload.get("removed", [])
    candidate_count = len(candidates) if isinstance(candidates, list) else 0
    removed_count = len(removed) if isinstance(removed, list) else 0
    reclaimable = _format_bytes(int(payload.get("reclaimable_bytes", 0)))
    if dry_run:
        summary = tr(
            "manage.gc.summary",
            locale,
            count=candidate_count,
            reclaimable=reclaimable,
        )
        return (
            f"[bold cyan]{tr('manage.gc.preview', locale)}[/bold cyan]  "
            f"[dim]{summary}[/dim]\n"
            f"[yellow]{tr('manage.gc.no_delete', locale)}[/yellow]"
        )
    receipt = escape(str(payload.get("receipt_path", "")))
    summary = tr(
        "manage.gc.summary",
        locale,
        count=removed_count,
        reclaimable=reclaimable,
    )
    return (
        f"[green]{tr('manage.gc.completed', locale)}[/green]  "
        f"[dim]{summary}[/dim]\n"
        f"[dim]{tr('manage.gc.receipt', locale, path=receipt)}[/dim]"
    )


# 将 MCP server 列表渲染为名称/传输/状态/工具数的紧凑清单
def render_mcp_servers(
    servers: list[dict[str, Any]], *, locale: str = "zh-CN"
) -> str:
    if not servers:
        return f"[dim]{tr('manage.mcp.empty', locale)}[/dim]"
    lines = [f"[bold cyan]{tr('manage.mcp.title', locale)}[/bold cyan]"]
    for server in servers:
        name = escape(str(server.get("name", "")))
        transport = escape(str(server.get("transport", "")))
        status = str(server.get("status", ""))
        marker = _STATUS_MARKERS.get(status, "[?]")
        tool_count = int(server.get("tool_count", 0))
        lines.append(
            f"{marker} {name}  [dim]{transport} · {status} · "
            f"{tr('manage.mcp.tools', locale, count=tool_count)}[/dim]"
        )
        error = str(server.get("error", "")).strip()
        if error:
            lines.append(f"    [red]{escape(_preview(error, 160))}[/red]")
    lines.append(f"[dim]{tr('manage.mcp.hint', locale)}[/dim]")
    return "\n".join(lines)


# 展开单个 MCP server 的工具清单，展示名称与描述
def render_mcp_tools(server: dict[str, Any], *, locale: str = "zh-CN") -> str:
    name = escape(str(server.get("name", "")))
    status = str(server.get("status", ""))
    lines = [
        f"[bold cyan]MCP {name}[/bold cyan]  [dim]{escape(status)}[/dim]"
    ]
    tools = server.get("tools", [])
    if not isinstance(tools, list) or not tools:
        lines.append(f"[dim]{tr('manage.mcp.no_tools', locale)}[/dim]")
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_name = escape(str(tool.get("name", "")))
        description = _preview(str(tool.get("description", "")), 100)
        desc_part = f"  [dim]{escape(description)}[/dim]" if description else ""
        lines.append(f"  • {tool_name}{desc_part}")
    return "\n".join(lines)


# 将 hook 配置表与最近执行记录渲染为结构化面板
def render_hooks(payload: dict[str, Any], *, locale: str = "zh-CN") -> str:
    configs = payload.get("configs", [])
    audit = payload.get("audit_events", [])
    if not isinstance(configs, list):
        configs = []
    if not isinstance(audit, list):
        audit = []
    lines: list[str] = [f"[bold cyan]{tr('manage.hooks.title', locale)}[/bold cyan]"]
    if not configs:
        lines.append(f"[dim]{tr('manage.hooks.empty', locale)}[/dim]")
    for config in configs:
        if not isinstance(config, dict):
            continue
        hook_id = escape(str(config.get("id", "")))
        event = escape(str(config.get("event", "")))
        blocking = config.get("blocking", False)
        scope = escape(str(config.get("trusted_scope", "")))
        mode = "block" if blocking else "async"
        lines.append(
            f"  [bold]{hook_id}[/bold]  [dim]{event} · {mode} · {scope}[/dim]"
        )
        command = config.get("command", [])
        if isinstance(command, list) and command:
            lines.append(f"      [dim]$ {escape(' '.join(map(str, command)))}[/dim]")
    if audit:
        lines.append(f"[bold cyan]{tr('manage.hooks.recent', locale)}[/bold cyan]")
        for record in audit:
            if not isinstance(record, dict):
                continue
            hook_id = escape(str(record.get("hook_id", "")))
            status = str(record.get("status", ""))
            marker = _STATUS_MARKERS.get(status, "[?]")
            elapsed = int(record.get("elapsed_ms", 0))
            ts = escape(str(record.get("ts", ""))[:19])
            reason = _preview(str(record.get("reason", "")), 120)
            reason_part = f"  [dim]{escape(reason)}[/dim]" if reason else ""
            lines.append(
                f"  {marker} {hook_id}  [dim]{escape(status)} · {elapsed}ms · {ts}[/dim]"
                f"{reason_part}"
            )
    lines.append(f"[dim]{tr('manage.hooks.hint', locale)}[/dim]")
    return "\n".join(lines)


# 将项目记忆条目和自动保存策略渲染为可操作清单
def render_memory(
    memories: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    *,
    locale: str = "zh-CN",
) -> str:
    auto_save = str((settings or {}).get("auto_save", "prompt"))
    lines = [
        f"[bold cyan]{tr('manage.memory.title', locale)}[/bold cyan]",
        f"[dim]{tr('manage.memory.auto', locale, mode=escape(auto_save))}[/dim]",
    ]
    if not memories:
        lines.append(f"[dim]{tr('manage.memory.empty', locale)}[/dim]")
        return "\n".join(lines)
    for memory in memories:
        memory_id = escape(str(memory.get("id", "")))
        name = escape(str(memory.get("name", "")))
        mem_type = str(memory.get("type", ""))
        pinned = "[yellow]★[/yellow] " if memory.get("pinned") is True else ""
        expired = (
            f" [red]{tr('manage.memory.expired', locale)}[/red]"
            if memory.get("expired") is True
            else ""
        )
        description = _preview(str(memory.get("description", "")), 120)
        desc_part = f"  [dim]{escape(description)}[/dim]" if description else ""
        lines.append(
            f"  {pinned}[{mem_type}] {name}  [dim]{memory_id}[/dim]{expired}{desc_part}"
        )
    lines.append(f"[dim]{tr('manage.memory.hint', locale)}[/dim]")
    return "\n".join(lines)


# 将后台 shell 任务列表渲染为任务中心视图
def render_jobs(
    background_jobs: list[dict[str, Any]], *, locale: str = "zh-CN"
) -> str:
    if not background_jobs:
        return f"[dim]{tr('manage.jobs.empty', locale)}[/dim]"
    lines = [f"[bold cyan]{tr('manage.jobs.title', locale)}[/bold cyan]"]
    for job in background_jobs:
        job_id = escape(str(job.get("id", "")))
        status = str(job.get("status", ""))
        marker = _STATUS_MARKERS.get(status, "[?]")
        command = _preview(str(job.get("command", "")), 76)
        lines.append(
            f"  {marker} {job_id}  [dim]{escape(status)}[/dim]  "
            f"[cyan]{escape(command)}[/cyan]"
        )
        output = str(job.get("output", "")).strip()
        preview = _preview(output, 200)
        if preview:
            lines.append(f"      [dim]{escape(preview)}[/dim]")
    lines.append(f"[dim]{tr('manage.jobs.hint', locale)}[/dim]")
    return "\n".join(lines)


# 渲染单个后台任务的全部增量输出与终态
def render_job_output(jobs: list[dict[str, Any]], *, locale: str = "zh-CN") -> str:
    if not jobs:
        return f"[dim]{tr('manage.jobs.missing', locale)}[/dim]"
    job = jobs[0]
    job_id = escape(str(job.get("id", "")))
    status = str(job.get("status", ""))
    command = escape(str(job.get("command", "")))
    output = str(job.get("output", ""))
    lines = [
        f"[bold cyan]{tr('manage.jobs.item_title', locale, id=job_id)}[/bold cyan]  "
        f"[dim]{escape(status)}[/dim]",
        f"[dim]$ {command}[/dim]",
        "",
        escape(output)
        if output
        else f"[dim]{tr('manage.jobs.no_output', locale)}[/dim]",
    ]
    return "\n".join(lines)


# 并行子代理/Worker 结果的统一汇总视图，折叠为一行一结果
def render_workers_summary(
    workers: list[dict[str, Any]], *, locale: str = "zh-CN"
) -> str:
    if not workers:
        return f"[dim]{tr('manage.workers.empty', locale)}[/dim]"
    lines = [f"[bold cyan]{tr('manage.workers.title', locale)}[/bold cyan]"]
    for worker in workers:
        worker_id = escape(str(worker.get("worker_id", "")))
        status = str(worker.get("status", ""))
        marker = _STATUS_MARKERS.get(status, "[?]")
        description = _preview(str(worker.get("description", "")), 72)
        lines.append(
            f"  {marker} {worker_id}  "
            f"[cyan]{escape(description)}[/cyan]  [dim]{escape(status)}[/dim]"
        )
        summary = _preview(str(worker.get("summary", "")), 160).replace("\n", " ")
        if summary:
            lines.append(f"      [dim]{escape(summary)}[/dim]")
    lines.append(f"[dim]{tr('manage.workers.hint', locale)}[/dim]")
    return "\n".join(lines)


# 截断单行文本到指定宽度（保留首个换行前的内容）
def _preview(text: str, max_len: int) -> str:
    if not text:
        return ""
    line = text.splitlines()[0]
    return line if len(line) <= max_len else line[:max_len] + "…"

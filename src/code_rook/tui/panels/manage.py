"""管理面板四件套（/mcp /hooks /memory /jobs）的纯渲染函数。

所有函数都是无副作用的富文本字符串构造，输入取自 IPC 返回的 dict 数据，
供 app.py 的看板方法直接 ``Static(body)`` 展示，也与面板单测解耦。
"""

from __future__ import annotations

from typing import Any

from rich.markup import escape

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


# 将 MCP server 列表渲染为名称/传输/状态/工具数的紧凑清单
def render_mcp_servers(servers: list[dict[str, Any]]) -> str:
    if not servers:
        return "[dim]当前没有配置 MCP server。[/dim]"
    lines = ["[bold cyan]MCP servers[/bold cyan]"]
    for server in servers:
        name = escape(str(server.get("name", "")))
        transport = escape(str(server.get("transport", "")))
        status = str(server.get("status", ""))
        marker = _STATUS_MARKERS.get(status, "[?]")
        tool_count = int(server.get("tool_count", 0))
        lines.append(
            f"{marker} {name}  [dim]{transport} · {status} · "
            f"{tool_count} tool(s)[/dim]"
        )
        error = str(server.get("error", "")).strip()
        if error:
            lines.append(f"    [red]{escape(_preview(error, 160))}[/red]")
    lines.append("[dim]使用 /mcp <name> 展开工具清单。[/dim]")
    return "\n".join(lines)


# 展开单个 MCP server 的工具清单，展示名称与描述
def render_mcp_tools(server: dict[str, Any]) -> str:
    name = escape(str(server.get("name", "")))
    status = str(server.get("status", ""))
    lines = [
        f"[bold cyan]MCP {name}[/bold cyan]  [dim]{escape(status)}[/dim]"
    ]
    tools = server.get("tools", [])
    if not isinstance(tools, list) or not tools:
        lines.append("[dim]该 server 未发现工具。[/dim]")
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_name = escape(str(tool.get("name", "")))
        description = _preview(str(tool.get("description", "")), 100)
        desc_part = f"  [dim]{escape(description)}[/dim]" if description else ""
        lines.append(f"  • {tool_name}{desc_part}")
    return "\n".join(lines)


# 将 hook 配置表与最近执行记录渲染为结构化面板
def render_hooks(payload: dict[str, Any]) -> str:
    configs = payload.get("configs", [])
    audit = payload.get("audit_events", [])
    if not isinstance(configs, list):
        configs = []
    if not isinstance(audit, list):
        audit = []
    lines: list[str] = ["[bold cyan]Hooks[/bold cyan]"]
    if not configs:
        lines.append("[dim]当前没有配置 hook。[/dim]")
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
        lines.append("[bold cyan]Recent executions[/bold cyan]")
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
    lines.append("[dim]使用 /hooks rerun <id> --yes 手动重跑。[/dim]")
    return "\n".join(lines)


# 将项目记忆条目列表渲染为带类型徽标与来源的清单
def render_memory(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "[dim]当前项目没有记忆条目。[/dim]"
    lines = ["[bold cyan]Project memory[/bold cyan]"]
    for memory in memories:
        memory_id = escape(str(memory.get("id", "")))
        name = escape(str(memory.get("name", "")))
        mem_type = str(memory.get("type", ""))
        description = _preview(str(memory.get("description", "")), 120)
        desc_part = f"  [dim]{escape(description)}[/dim]" if description else ""
        lines.append(
            f"  [{mem_type}] {name}  [dim]{memory_id}[/dim]{desc_part}"
        )
    lines.append("[dim]使用 /memory delete <id> --yes 删除。[/dim]")
    return "\n".join(lines)


# 将后台 shell 任务列表渲染为任务中心视图
def render_jobs(background_jobs: list[dict[str, Any]]) -> str:
    if not background_jobs:
        return "[dim]当前没有后台任务。[/dim]"
    lines = ["[bold cyan]Background jobs[/bold cyan]"]
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
    lines.append(
        "[dim]使用 /jobs show <id> 查看增量输出，/jobs cancel <id> --yes 取消。[/dim]"
    )
    return "\n".join(lines)


# 渲染单个后台任务的全部增量输出与终态
def render_job_output(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return "[dim]未找到该任务。[/dim]"
    job = jobs[0]
    job_id = escape(str(job.get("id", "")))
    status = str(job.get("status", ""))
    command = escape(str(job.get("command", "")))
    output = str(job.get("output", ""))
    lines = [
        f"[bold cyan]Job {job_id}[/bold cyan]  [dim]{escape(status)}[/dim]",
        f"[dim]$ {command}[/dim]",
        "",
        escape(output) if output else "[dim][no output][/dim]",
    ]
    return "\n".join(lines)


# 并行子代理/Worker 结果的统一汇总视图，折叠为一行一结果
def render_workers_summary(workers: list[dict[str, Any]]) -> str:
    if not workers:
        return "[dim]没有并行子代理。[/dim]"
    lines = ["[bold cyan]Subagents / workers[/bold cyan]"]
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
    lines.append("[dim]使用 /jobs cancel <worker_id> --yes 取消子代理。[/dim]")
    return "\n".join(lines)


# 截断单行文本到指定宽度（保留首个换行前的内容）
def _preview(text: str, max_len: int) -> str:
    if not text:
        return ""
    line = text.splitlines()[0]
    return line if len(line) <= max_len else line[:max_len] + "…"
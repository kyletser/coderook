from __future__ import annotations

import json
from typing import Any

from rich.markup import escape


# 将结构化值压缩成适合单行 inspector 的安全预览
def _preview(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return escape(text)


# 渲染 route、authority、usage、tools、approvals、evidence 与 receipt 摘要
def render_turn_inspector(payload: dict[str, Any]) -> str:
    turn = dict(payload.get("turn", {}))
    receipt = dict(payload.get("receipt", {}))
    items = [dict(item) for item in payload.get("items", [])]
    events = [dict(event) for event in payload.get("events", [])]
    route = turn.get("route")
    route_data = dict(route) if isinstance(route, dict) else {}
    usage = dict(turn.get("usage", {}))
    authority = dict(receipt.get("authority", {}))
    sandbox = dict(authority.get("sandbox", {}))
    sandbox_plan = dict(authority.get("sandbox_plan", {}))
    approvals = dict(receipt.get("approvals", {}))
    user_messages = [
        dict(item.get("payload", {}))
        for item in items
        if item.get("kind") == "message"
        and dict(item.get("payload", {})).get("role") == "user"
    ]
    goal = user_messages[0].get("content", "unavailable") if user_messages else "unavailable"
    workers = list(receipt.get("workers", []))
    worker_started = sum(
        event.get("type") in {"subagent.started", "worker.started"} for event in events
    )
    worker_finished = sum(
        event.get("type") in {"subagent.finished", "worker.finished"}
        for event in events
    )
    if worker_started:
        active_workers = max(worker_started - worker_finished, 0)
        total_workers = worker_started
    else:
        active_workers = sum(
            str(dict(worker).get("status", "")).lower()
            in {"queued", "running", "started", "waiting"}
            for worker in workers
            if isinstance(worker, dict)
        )
        total_workers = len(workers)
    pending_approvals = max(
        int(approvals.get("requested", 0))
        - int(approvals.get("granted", 0))
        - int(approvals.get("denied", 0)),
        0,
    )
    failure = receipt.get("error_classification") or "none"
    process_usage = dict(receipt.get("process_usage", {}))
    process_cpu_ms = int(process_usage.get("user_cpu_ms", 0)) + int(
        process_usage.get("system_cpu_ms", 0)
    )
    lines = [
        f"[bold cyan]Turn Inspector[/bold cyan]  {escape(str(turn.get('id', '-')))}  "
        f"[bold]{escape(str(turn.get('status', 'unknown')))}[/bold]",
        f"goal={_preview(goal, 160)}",
        (
            f"workers={active_workers}/{total_workers} active/total  "
            f"cost={escape(str(receipt.get('cost', 'unknown')))}  "
            f"pending_approvals={pending_approvals}  "
            f"failure={escape(str(failure))}"
        ),
        (
            f"route={escape(str(route_data.get('route_id', 'unavailable')))}  "
            f"model={escape(str(route_data.get('model', 'unavailable')))}  "
            f"wire={escape(str(route_data.get('wire_format', 'unavailable')))}"
        ),
        (
            f"mode={escape(str(authority.get('mode', '-')))}  "
            f"authority={escape(str(authority.get('profile', '-')))}  "
            f"trust={escape(str(authority.get('workspace_trust', '-')))}  "
            "sandbox="
            f"{escape(str(sandbox_plan.get('backend', sandbox.get('kind', 'unavailable'))))}"
        ),
        f"usage {_preview(usage)}  cost={escape(str(receipt.get('cost', 'unknown')))}",
        (
            "processes="
            f"{int(process_usage.get('process_count', 0))}  "
            f"cpu={process_cpu_ms}ms  "
            f"peak_memory={int(process_usage.get('peak_memory_bytes', 0))}B  "
            f"samples={int(process_usage.get('complete_records', 0))}/"
            f"{int(process_usage.get('record_count', 0))} complete"
        ),
        (
            f"tools={int(receipt.get('tool_call_count', 0))}  approvals="
            f"{int(approvals.get('requested', 0))}/"
            f"{int(approvals.get('granted', 0))}/"
            f"{int(approvals.get('denied', 0))} [dim](asked/allowed/denied)[/dim]"
        ),
    ]
    tool_items = [item for item in items if item.get("kind") == "tool_call"]
    for item in tool_items:
        item_payload = dict(item.get("payload", {}))
        lines.append(
            f"  [dim]›[/dim] {escape(str(item_payload.get('tool_name', 'tool')))}  "
            f"{_preview(item_payload.get('params', {}), 90)}"
        )
    diagnostics = [event for event in events if event.get("type") == "lsp.diagnostics"]
    if diagnostics:
        lines.append(f"verification {_preview(diagnostics[-1].get('payload', {}))}")
    for label in ("files_changed", "checkpoints", "artifacts", "workers"):
        value = receipt.get(label, [])
        if value:
            lines.append(f"{label} {_preview(value)}")
    unavailable = receipt.get("unavailable", [])
    if unavailable:
        lines.append(f"[dim]unavailable: {escape(', '.join(map(str, unavailable)))}[/dim]")
    error = receipt.get("error_classification")
    if error:
        lines.append(f"[red]error={escape(str(error))}[/red]")
    return "\n".join(lines)

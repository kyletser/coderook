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
    approvals = dict(receipt.get("approvals", {}))
    lines = [
        f"[bold cyan]Turn Inspector[/bold cyan]  {escape(str(turn.get('id', '-')))}  "
        f"[bold]{escape(str(turn.get('status', 'unknown')))}[/bold]",
        (
            f"route={escape(str(route_data.get('route_id', 'unavailable')))}  "
            f"model={escape(str(route_data.get('model', 'unavailable')))}  "
            f"wire={escape(str(route_data.get('wire_format', 'unavailable')))}"
        ),
        (
            f"mode={escape(str(authority.get('mode', '-')))}  "
            f"authority={escape(str(authority.get('profile', '-')))}  "
            f"trust={escape(str(authority.get('workspace_trust', '-')))}  "
            f"sandbox={escape(str(sandbox.get('kind', 'unavailable')))}"
        ),
        f"usage {_preview(usage)}  cost={escape(str(receipt.get('cost', 'unknown')))}",
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

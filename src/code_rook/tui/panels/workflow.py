from __future__ import annotations

from typing import Any

from rich.markup import escape

_STATUS_MARKERS = {
    "pending": "[ ]",
    "running": "[>]",
    "completed": "[x]",
    "failed": "[!]",
    "blocked": "[-]",
    "skipped": "[~]",
    "interrupted": "[|]",
    "budget_limited": "[$]",
}


# 将 workflow 列表渲染为紧凑可复制的 durable projection
def render_workflow_list(workflows: list[dict[str, Any]]) -> str:
    if not workflows:
        return "[dim]当前没有 durable workflow。[/dim]"
    lines = ["[bold cyan]Workflows[/bold cyan]"]
    for workflow in workflows:
        status = str(workflow.get("status", "pending"))
        workflow_id = escape(str(workflow.get("id", "")))
        updated = escape(str(workflow.get("updated_at", "")))
        lines.append(
            f"{_STATUS_MARKERS.get(status, '[?]')} {workflow_id}  "
            f"[dim]{escape(status)} · {updated}[/dim]"
        )
    lines.append("[dim]使用 /workflow <id> 展开 Work Graph。[/dim]")
    return "\n".join(lines)


# 将 reducer 生成的 Work Graph 渲染为 Task/Worker/Gate/Artifact 投影
def render_workflow_graph(graph: dict[str, Any]) -> str:
    workflow_id = escape(str(graph.get("workflow_id", "")))
    status = str(graph.get("status", "pending"))
    cursor = graph.get("cursor", 0)
    lines = [
        f"[bold cyan]Workflow {workflow_id}[/bold cyan]  "
        f"[dim]{escape(status)} · event {cursor}[/dim]"
    ]
    raw_nodes = graph.get("nodes", {})
    nodes = raw_nodes if isinstance(raw_nodes, dict) else {}
    order = {"goal": 0, "task": 1, "worker": 2, "gate": 3, "artifact": 4}
    sorted_nodes = sorted(
        (node for node in nodes.values() if isinstance(node, dict)),
        key=lambda node: (
            order.get(str(node.get("kind", "task")), 9),
            str(node.get("id", "")),
        ),
    )
    for node in sorted_nodes:
        node_status = str(node.get("status", "pending"))
        node_kind = escape(str(node.get("kind", "task")))
        node_id = escape(str(node.get("id", "")))
        attempt = node.get("attempt", 0)
        suffix = f" · attempt {attempt}" if attempt else ""
        lines.append(
            f"{_STATUS_MARKERS.get(node_status, '[?]')} "
            f"[dim]{node_kind}[/dim] {node_id}  "
            f"[dim]{escape(node_status)}{suffix}[/dim]"
        )
        summary = str(node.get("summary", "")).strip()
        if summary:
            lines.append(f"    [dim]{escape(summary[:240])}[/dim]")
    raw_edges = graph.get("edges", [])
    edges = raw_edges if isinstance(raw_edges, list) else []
    if edges:
        lines.append(f"[dim]Edges · {len(edges)}[/dim]")
        for edge in edges[:50]:
            if not isinstance(edge, dict):
                continue
            source = escape(str(edge.get("source", "")))
            target = escape(str(edge.get("target", "")))
            kind = escape(str(edge.get("kind", "")))
            lines.append(f"  {source} -[{kind}]-> {target}")
    return "\n".join(lines)

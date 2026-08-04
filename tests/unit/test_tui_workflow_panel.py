from __future__ import annotations

from code_rook.tui.panels import render_workflow_graph, render_workflow_list


# 功能：Workflow 列表 projection 展示 durable ID、状态和展开提示
# 设计：传入两个不同终态，检查紧凑标记而不依赖 Textual app 生命周期
def test_workflow_list_projection_is_compact() -> None:
    rendered = render_workflow_list(
        [
            {"id": "release", "status": "running", "updated_at": "2026-08-04"},
            {"id": "verify", "status": "completed", "updated_at": "2026-08-04"},
        ]
    )

    assert "[>] release" in rendered
    assert "[x] verify" in rendered
    assert "/workflow <id>" in rendered


# 功能：Workflow graph projection 按节点类型展示状态、attempt、摘要和 typed edge
# 设计：直接输入 reducer JSON 形状，验证视图只投影 durable state 而无私有 executor 依赖
def test_workflow_graph_projection_renders_nodes_and_edges() -> None:
    rendered = render_workflow_graph(
        {
            "workflow_id": "release",
            "status": "running",
            "cursor": 12,
            "nodes": {
                "build": {
                    "id": "build",
                    "kind": "worker",
                    "status": "completed",
                    "attempt": 1,
                    "summary": "artifact built",
                },
                "review": {
                    "id": "review",
                    "kind": "gate",
                    "status": "running",
                    "attempt": 1,
                },
            },
            "edges": [
                {"source": "review", "target": "build", "kind": "reviews"}
            ],
        }
    )

    assert "Workflow release" in rendered
    assert "worker[/dim] build" in rendered
    assert "artifact built" in rendered
    assert "review -[reviews]-> build" in rendered

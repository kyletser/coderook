from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.workflow import (
    GraphEdgeKind,
    GraphNodeKind,
    WorkflowLedger,
    WorkflowLedgerError,
    WorkflowNodeStatus,
    WorkflowSpec,
    parse_workflow_text,
)


# 构造包含两个顺序 Worker 的最小 WorkflowSpec
def _spec() -> WorkflowSpec:
    return parse_workflow_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "workflow-ledger",
                "name": "ledger workflow",
                "root": {
                    "type": "sequence",
                    "id": "root",
                    "steps": [
                        {
                            "type": "worker",
                            "id": "worker-a",
                            "description": "worker a",
                            "prompt": "do a",
                        },
                        {
                            "type": "worker",
                            "id": "worker-b",
                            "description": "worker b",
                            "prompt": "do b",
                        },
                    ],
                },
            }
        ),
        format="json",
    )


# 功能：Work Graph 完全由严格递增 durable event reducer 重建
# 设计：在 SQLite ledger 追加节点、依赖边和结果事件，再从新实例离线投影图状态
def test_ledger_reduces_nodes_edges_and_results(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    ledger = WorkflowLedger(path)
    ledger.create(_spec())
    ledger.append("workflow-ledger", "workflow.started")
    ledger.append(
        "workflow-ledger",
        "node.registered",
        node_id="worker-a",
        node_kind=GraphNodeKind.WORKER,
    )
    ledger.append(
        "workflow-ledger",
        "node.registered",
        node_id="worker-b",
        node_kind=GraphNodeKind.WORKER,
    )
    ledger.append(
        "workflow-ledger",
        "edge.registered",
        details={
            "source": "worker-b",
            "target": "worker-a",
            "edge_kind": "depends_on",
        },
    )
    ledger.append("workflow-ledger", "node.started", node_id="worker-a")
    ledger.append(
        "workflow-ledger",
        "node.completed",
        node_id="worker-a",
        details={
            "summary": "done a",
            "evidence": ["test:a"],
            "token_usage": 10,
        },
    )

    restored = WorkflowLedger(path).graph("workflow-ledger")

    assert restored.status == "running"
    assert restored.nodes["worker-a"].status == WorkflowNodeStatus.COMPLETED
    assert restored.nodes["worker-a"].summary == "done a"
    assert restored.nodes["worker-a"].evidence == ["test:a"]
    assert restored.edges[0].kind == GraphEdgeKind.DEPENDS_ON


# 功能：Core 重启恢复只中断 running 节点，不重复或降级 completed 节点
# 设计：一个节点完成、另一个运行后重开 ledger 并 recover，验证两种状态分别保持和转换
def test_recovery_preserves_completed_and_interrupts_running(tmp_path: Path) -> None:
    path = tmp_path / "workflow.db"
    ledger = WorkflowLedger(path)
    ledger.create(_spec())
    ledger.append("workflow-ledger", "workflow.started")
    for worker_id in ("worker-a", "worker-b"):
        ledger.append(
            "workflow-ledger",
            "node.registered",
            node_id=worker_id,
            node_kind=GraphNodeKind.WORKER,
        )
        ledger.append("workflow-ledger", "node.started", node_id=worker_id)
    ledger.append("workflow-ledger", "node.completed", node_id="worker-a")

    recovered = WorkflowLedger(path).recover_interrupted("workflow-ledger")

    assert recovered.status == "interrupted"
    assert recovered.nodes["worker-a"].status == WorkflowNodeStatus.COMPLETED
    assert recovered.nodes["worker-b"].status == WorkflowNodeStatus.INTERRUPTED
    assert [event.kind for event in ledger.events("workflow-ledger")].count(
        "node.completed"
    ) == 1


# 功能：相同 ID 的不同 WorkflowSpec 不能覆盖既有 ledger 定义
# 设计：先持久有效 spec，再更改 name 但保留 ID，断言 create 拒绝定义漂移
def test_ledger_rejects_definition_drift(tmp_path: Path) -> None:
    ledger = WorkflowLedger(tmp_path / "workflow.db")
    spec = _spec()
    ledger.create(spec)

    with pytest.raises(WorkflowLedgerError, match="already exists"):
        ledger.create(spec.model_copy(update={"name": "changed"}))


# 功能：artifact event 同时生成 artifact 节点与 produces 边
# 设计：先注册 producer，再追加 artifact handle，验证 reducer 的图关系而非复制产物正文
def test_artifact_event_creates_produces_edge(tmp_path: Path) -> None:
    ledger = WorkflowLedger(tmp_path / "workflow.db")
    ledger.create(_spec())
    ledger.append(
        "workflow-ledger",
        "node.registered",
        node_id="worker-a",
        node_kind=GraphNodeKind.WORKER,
    )
    ledger.append(
        "workflow-ledger",
        "artifact.registered",
        node_id="artifact-a",
        details={"producer": "worker-a", "handle": "artifact://sha256/abc"},
    )

    graph = ledger.graph("workflow-ledger")

    assert graph.nodes["artifact-a"].kind == GraphNodeKind.ARTIFACT
    assert graph.nodes["artifact-a"].artifact_handles == ["artifact://sha256/abc"]
    assert graph.edges[-1].kind == GraphEdgeKind.PRODUCES

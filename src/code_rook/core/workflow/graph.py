from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class GraphNodeKind(StrEnum):
    GOAL = "goal"
    TASK = "task"
    WORKER = "worker"
    GATE = "gate"
    ARTIFACT = "artifact"


class GraphEdgeKind(StrEnum):
    DEPENDS_ON = "depends_on"
    PRODUCES = "produces"
    REVIEWS = "reviews"
    BLOCKS = "blocks"


class WorkflowNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"
    BUDGET_LIMITED = "budget_limited"


class WorkGraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: GraphNodeKind
    status: WorkflowNodeStatus = WorkflowNodeStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    summary: str = ""
    evidence: list[str] = Field(default_factory=list)
    artifact_handles: list[str] = Field(default_factory=list)
    token_usage: int = Field(default=0, ge=0)
    approved: bool | None = None
    receipt: dict[str, JsonValue] = Field(default_factory=dict)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class WorkGraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: GraphEdgeKind


WorkflowEventKind = Literal[
    "workflow.started",
    "workflow.completed",
    "workflow.failed",
    "workflow.interrupted",
    "node.registered",
    "node.started",
    "node.completed",
    "node.failed",
    "node.blocked",
    "node.skipped",
    "node.interrupted",
    "node.budget_limited",
    "edge.registered",
    "artifact.registered",
    "gate.passed",
    "gate.failed",
]


class WorkflowEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    kind: WorkflowEventKind
    node_id: str = ""
    node_kind: GraphNodeKind | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)
    at: str


class WorkGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    status: Literal[
        "pending",
        "running",
        "completed",
        "failed",
        "interrupted",
    ] = "pending"
    cursor: int = Field(default=0, ge=0)
    nodes: dict[str, WorkGraphNode] = Field(default_factory=dict)
    edges: list[WorkGraphEdge] = Field(default_factory=list)
    receipt: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowReductionError(ValueError):
    pass


# 从事件 details 读取字符串字段并在缺失时 fail closed
def _required_string(event: WorkflowEvent, key: str) -> str:
    value = event.details.get(key)
    if not isinstance(value, str) or not value:
        raise WorkflowReductionError(f"{event.kind} requires details.{key}")
    return value


# 获取已注册节点，拒绝对未知节点应用状态事件
def _node(state: WorkGraphState, event: WorkflowEvent) -> WorkGraphNode:
    node = state.nodes.get(event.node_id)
    if node is None:
        raise WorkflowReductionError(f"unknown workflow node: {event.node_id}")
    return node


# 将一条严格递增事件纯函数应用到 Work Graph 投影
def apply_workflow_event(
    state: WorkGraphState,
    event: WorkflowEvent,
) -> WorkGraphState:
    if event.workflow_id != state.workflow_id:
        raise WorkflowReductionError("workflow event belongs to another workflow")
    if event.seq <= state.cursor:
        return state.model_copy(deep=True)
    if event.seq != state.cursor + 1:
        raise WorkflowReductionError(
            f"workflow event sequence gap: expected {state.cursor + 1}, got {event.seq}"
        )
    updated = state.model_copy(deep=True)
    if event.kind == "workflow.started":
        updated.status = "running"
    elif event.kind == "workflow.completed":
        updated.status = "completed"
        receipt = event.details.get("receipt")
        if isinstance(receipt, dict):
            updated.receipt = receipt
    elif event.kind == "workflow.failed":
        updated.status = "failed"
    elif event.kind == "workflow.interrupted":
        updated.status = "interrupted"
    elif event.kind == "node.registered":
        if not event.node_id or event.node_kind is None:
            raise WorkflowReductionError("node.registered requires node_id and node_kind")
        if event.node_id in updated.nodes:
            raise WorkflowReductionError(f"duplicate workflow node: {event.node_id}")
        updated.nodes[event.node_id] = WorkGraphNode(
            id=event.node_id,
            kind=event.node_kind,
            metadata=event.details,
        )
    elif event.kind == "edge.registered":
        source = _required_string(event, "source")
        target = _required_string(event, "target")
        raw_kind = _required_string(event, "edge_kind")
        if source not in updated.nodes or target not in updated.nodes:
            raise WorkflowReductionError("edge endpoints must be registered first")
        try:
            edge_kind = GraphEdgeKind(raw_kind)
        except ValueError:
            raise WorkflowReductionError(f"unknown graph edge kind: {raw_kind}") from None
        edge = WorkGraphEdge(source=source, target=target, kind=edge_kind)
        if edge not in updated.edges:
            updated.edges.append(edge)
    elif event.kind == "artifact.registered":
        if not event.node_id:
            raise WorkflowReductionError("artifact.registered requires node_id")
        producer = _required_string(event, "producer")
        handle = _required_string(event, "handle")
        if producer not in updated.nodes:
            raise WorkflowReductionError(f"unknown artifact producer: {producer}")
        updated.nodes[event.node_id] = WorkGraphNode(
            id=event.node_id,
            kind=GraphNodeKind.ARTIFACT,
            status=WorkflowNodeStatus.COMPLETED,
            artifact_handles=[handle],
            metadata=event.details,
        )
        edge = WorkGraphEdge(
            source=producer,
            target=event.node_id,
            kind=GraphEdgeKind.PRODUCES,
        )
        if edge not in updated.edges:
            updated.edges.append(edge)
    else:
        node = _node(updated, event)
        if event.kind == "node.started":
            node.status = WorkflowNodeStatus.RUNNING
            raw_attempt = event.details.get("attempt", 0)
            requested_attempt = raw_attempt if isinstance(raw_attempt, int) else 0
            node.attempt = max(node.attempt + 1, requested_attempt)
        elif event.kind == "node.completed":
            node.status = WorkflowNodeStatus.COMPLETED
        elif event.kind == "node.failed":
            node.status = WorkflowNodeStatus.FAILED
        elif event.kind == "node.blocked":
            node.status = WorkflowNodeStatus.BLOCKED
        elif event.kind == "node.skipped":
            node.status = WorkflowNodeStatus.SKIPPED
        elif event.kind == "node.interrupted":
            node.status = WorkflowNodeStatus.INTERRUPTED
        elif event.kind == "node.budget_limited":
            node.status = WorkflowNodeStatus.BUDGET_LIMITED
        elif event.kind == "gate.passed":
            node.status = WorkflowNodeStatus.COMPLETED
            node.approved = True
        elif event.kind == "gate.failed":
            node.status = WorkflowNodeStatus.FAILED
            node.approved = False
        summary = event.details.get("summary")
        if isinstance(summary, str):
            node.summary = summary[:4_000]
        evidence = event.details.get("evidence")
        if isinstance(evidence, list):
            node.evidence = [str(item)[:500] for item in evidence[:50]]
        artifacts = event.details.get("artifact_handles")
        if isinstance(artifacts, list):
            node.artifact_handles = [str(item)[:500] for item in artifacts[:50]]
        token_usage = event.details.get("token_usage")
        if isinstance(token_usage, int) and token_usage >= 0:
            node.token_usage = token_usage
        receipt = event.details.get("receipt")
        if isinstance(receipt, dict):
            node.receipt = receipt
    updated.cursor = event.seq
    return updated


# 从完整 durable event 流重建 Workflow Work Graph 投影
def reduce_workflow_events(
    workflow_id: str,
    events: list[WorkflowEvent],
) -> WorkGraphState:
    state = WorkGraphState(workflow_id=workflow_id)
    for event in sorted(events, key=lambda item: item.seq):
        state = apply_workflow_event(state, event)
    return state

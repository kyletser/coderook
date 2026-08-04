from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from code_rook.core.workflow.graph import (
    GraphEdgeKind,
    GraphNodeKind,
    WorkflowNodeStatus,
    WorkGraphNode,
    WorkGraphState,
)
from code_rook.core.workflow.ledger import WorkflowLedger
from code_rook.core.workflow.models import (
    BranchStep,
    FanInStep,
    ParallelStep,
    RetryStep,
    ReviewGateStep,
    SequenceStep,
    WorkerStep,
    WorkflowSpec,
    WorkflowStep,
)


class WorkerExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str = Field(pattern=r"^(completed|failed|budget_limited)$")
    summary: str = ""
    evidence: list[str] = Field(default_factory=list, max_length=50)
    artifact_handles: list[str] = Field(default_factory=list, max_length=50)
    token_usage: int = Field(default=0, ge=0)
    approved: bool | None = None
    receipt: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowWorkerAdapter(Protocol):
    # 执行一个声明式 WorkerStep 并返回有界结构化结果
    async def run_worker(
        self,
        workflow_id: str,
        step: WorkerStep,
        *,
        attempt: int,
    ) -> WorkerExecutionResult: ...


class WorkflowReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    workflow_id: str
    status: str
    spec_digest: str
    input_digest: str
    configuration_digest: str
    node_receipts: list[dict[str, JsonValue]] = Field(default_factory=list)
    execution_digest: str


@dataclass(frozen=True)
class _StepOutcome:
    node_id: str
    success: bool
    status: WorkflowNodeStatus
    summary: str = ""
    evidence: tuple[str, ...] = ()
    token_usage: int = 0
    approved: bool | None = None


# 将 JSON 兼容值编码为稳定 canonical 字节
def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


# 返回 canonical JSON 的 SHA-256 摘要
def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


# 返回节点的直接子步骤
def _children(step: WorkflowStep) -> list[WorkflowStep]:
    if isinstance(step, (SequenceStep, ParallelStep, FanInStep)):
        return list(step.steps)
    if isinstance(step, BranchStep):
        return [step.then_step, step.else_step]
    if isinstance(step, RetryStep):
        return [step.step]
    if isinstance(step, ReviewGateStep):
        return [step.step, step.reviewer]
    return []


# 深度优先返回完整 Workflow IR 节点列表
def _flatten(step: WorkflowStep) -> list[WorkflowStep]:
    flattened = [step]
    for child in _children(step):
        flattened.extend(_flatten(child))
    return flattened


# 将 IR 节点类型映射为 Work Graph 节点类型
def _node_kind(step: WorkflowStep) -> GraphNodeKind:
    if isinstance(step, WorkerStep):
        return GraphNodeKind.WORKER
    if isinstance(step, ReviewGateStep):
        return GraphNodeKind.GATE
    return GraphNodeKind.TASK


class WorkflowExecutor:
    # 绑定 durable ledger 与本地 Worker adapter
    def __init__(self, ledger: WorkflowLedger, adapter: WorkflowWorkerAdapter) -> None:
        self._ledger = ledger
        self._adapter = adapter
        self._spec: WorkflowSpec | None = None
        self._tokens_used = 0
        self._global_semaphore = asyncio.Semaphore(1)

    # 注册 Goal/Task/Worker/Gate 节点与静态依赖边，重启时保持幂等
    def _ensure_graph_registered(self, spec: WorkflowSpec) -> None:
        state = self._ledger.graph(spec.id)
        goal_id = f"goal:{spec.id}"
        if goal_id not in state.nodes:
            self._ledger.append(
                spec.id,
                "node.registered",
                node_id=goal_id,
                node_kind=GraphNodeKind.GOAL,
                details={"name": spec.name},
            )
        for step in _flatten(spec.root):
            state = self._ledger.graph(spec.id)
            if step.id in state.nodes:
                continue
            details: dict[str, JsonValue] = {"step_type": step.type}
            if isinstance(step, WorkerStep):
                details.update(
                    {
                        "profile": step.profile,
                        "route": step.route,
                        "model": step.model,
                        "reasoning": step.reasoning,
                    }
                )
            self._ledger.append(
                spec.id,
                "node.registered",
                node_id=step.id,
                node_kind=_node_kind(step),
                details=details,
            )
        self._append_edge(spec.id, goal_id, spec.root.id, GraphEdgeKind.DEPENDS_ON)
        for step in _flatten(spec.root):
            children = _children(step)
            if isinstance(step, SequenceStep):
                for previous, current in zip(children, children[1:], strict=False):
                    self._append_edge(
                        spec.id,
                        current.id,
                        previous.id,
                        GraphEdgeKind.DEPENDS_ON,
                    )
            for child in children:
                self._append_edge(
                    spec.id,
                    step.id,
                    child.id,
                    GraphEdgeKind.DEPENDS_ON,
                )
            if isinstance(step, ReviewGateStep):
                self._append_edge(
                    spec.id,
                    step.reviewer.id,
                    step.step.id,
                    GraphEdgeKind.REVIEWS,
                )

    # 仅在边尚不存在时追加静态 Work Graph edge
    def _append_edge(
        self,
        workflow_id: str,
        source: str,
        target: str,
        kind: GraphEdgeKind,
    ) -> None:
        state = self._ledger.graph(workflow_id)
        if any(
            edge.source == source and edge.target == target and edge.kind == kind
            for edge in state.edges
        ):
            return
        self._ledger.append(
            workflow_id,
            "edge.registered",
            details={"source": source, "target": target, "edge_kind": kind.value},
        )

    # 执行或恢复 workflow；已完成节点直接复用 durable graph 结果
    async def run(self, spec: WorkflowSpec) -> WorkGraphState:
        self._ledger.create(spec)
        state = self._ledger.graph(spec.id)
        if state.status == "completed":
            return state
        if state.status == "running":
            state = self._ledger.recover_interrupted(spec.id)
        self._ensure_graph_registered(spec)
        self._spec = spec
        state = self._ledger.graph(spec.id)
        self._tokens_used = self._recorded_token_usage(spec.id)
        self._global_semaphore = asyncio.Semaphore(spec.limits.max_concurrency)
        self._ledger.append(spec.id, "workflow.started")
        goal_id = f"goal:{spec.id}"
        goal = self._ledger.graph(spec.id).nodes[goal_id]
        if goal.status != WorkflowNodeStatus.COMPLETED:
            self._ledger.append(spec.id, "node.started", node_id=goal_id)
        try:
            async with asyncio.timeout(spec.limits.wall_time_s):
                outcome = await self._run_step(spec.root)
        except TimeoutError:
            self._interrupt_running(spec.id, "workflow wall-time exceeded")
            self._ledger.append(
                spec.id,
                "workflow.failed",
                details={"reason": "wall_time_exceeded"},
            )
            return self._ledger.graph(spec.id)
        if outcome.success:
            self._ledger.append(
                spec.id,
                "node.completed",
                node_id=goal_id,
                details={"summary": "workflow goal completed"},
            )
            receipt = self._build_receipt(spec, "completed")
            self._ledger.append(
                spec.id,
                "workflow.completed",
                details={
                    "receipt": cast(dict[str, JsonValue], receipt.model_dump(mode="json"))
                },
            )
        else:
            self._ledger.append(
                spec.id,
                "node.failed",
                node_id=goal_id,
                details={"summary": f"blocked by {outcome.node_id}"},
            )
            self._ledger.append(
                spec.id,
                "workflow.failed",
                details={"reason": outcome.status.value, "node_id": outcome.node_id},
            )
        return self._ledger.graph(spec.id)

    # 汇总 durable 终态事件中的 token 使用量，避免重启和 retry 后预算回退
    def _recorded_token_usage(self, workflow_id: str, node_id: str = "") -> int:
        total = 0
        terminal_kinds = {
            "node.completed",
            "node.failed",
            "node.budget_limited",
        }
        for event in self._ledger.events(workflow_id):
            if event.kind not in terminal_kinds:
                continue
            if node_id and event.node_id != node_id:
                continue
            usage = event.details.get("token_usage")
            if isinstance(usage, int) and usage >= 0:
                total += usage
        return total

    # 把 workflow 当前 running 节点转换为 interrupted
    def _interrupt_running(self, workflow_id: str, reason: str) -> None:
        state = self._ledger.graph(workflow_id)
        for node in state.nodes.values():
            if node.status == WorkflowNodeStatus.RUNNING:
                self._ledger.append(
                    workflow_id,
                    "node.interrupted",
                    node_id=node.id,
                    details={"reason": reason},
                )

    # 读取节点 durable 终态并转换为执行器结果
    def _restored_outcome(self, node: WorkGraphNode) -> _StepOutcome:
        return _StepOutcome(
            node_id=node.id,
            success=node.status in {
                WorkflowNodeStatus.COMPLETED,
                WorkflowNodeStatus.SKIPPED,
            },
            status=node.status,
            summary=node.summary,
            evidence=tuple(node.evidence),
            token_usage=node.token_usage,
            approved=node.approved,
        )

    # 按具体 IR 节点类型分派执行，completed/skipped 节点绝不重复运行
    async def _run_step(self, step: WorkflowStep) -> _StepOutcome:
        assert self._spec is not None
        state = self._ledger.graph(self._spec.id)
        existing = state.nodes[step.id]
        if existing.status in {
            WorkflowNodeStatus.COMPLETED,
            WorkflowNodeStatus.SKIPPED,
        }:
            return self._restored_outcome(existing)
        if isinstance(step, WorkerStep):
            return await self._run_worker(step)
        self._ledger.append(self._spec.id, "node.started", node_id=step.id)
        if isinstance(step, SequenceStep):
            return await self._run_sequence(step)
        if isinstance(step, ParallelStep):
            return await self._run_parallel(step)
        if isinstance(step, BranchStep):
            return await self._run_branch(step)
        if isinstance(step, RetryStep):
            return await self._run_retry(step)
        if isinstance(step, ReviewGateStep):
            return await self._run_review_gate(step)
        if isinstance(step, FanInStep):
            return await self._run_fan_in(step)
        raise TypeError(f"unsupported workflow step: {type(step).__name__}")

    # 执行 WorkerStep 并统一记录 token、artifact、evidence 和 receipt
    async def _run_worker(self, step: WorkerStep) -> _StepOutcome:
        assert self._spec is not None
        budget = self._spec.limits.token_budget
        node_usage = self._recorded_token_usage(self._spec.id, step.id)
        budget_exhausted = budget is not None and self._tokens_used >= budget
        node_budget_exhausted = (
            step.token_budget is not None and node_usage >= step.token_budget
        )
        if budget_exhausted or node_budget_exhausted:
            reason = (
                "workflow token budget exhausted"
                if budget_exhausted
                else "worker token budget exhausted"
            )
            self._ledger.append(
                self._spec.id,
                "node.budget_limited",
                node_id=step.id,
                details={"token_usage": 0, "reason": reason},
            )
            return _StepOutcome(
                step.id,
                False,
                WorkflowNodeStatus.BUDGET_LIMITED,
            )
        current = self._ledger.graph(self._spec.id).nodes[step.id]
        attempt = current.attempt + 1
        self._ledger.append(
            self._spec.id,
            "node.started",
            node_id=step.id,
            details={"attempt": attempt},
        )
        try:
            async with self._global_semaphore:
                async with asyncio.timeout(step.wall_time_s):
                    result = await self._adapter.run_worker(
                        self._spec.id,
                        step,
                        attempt=attempt,
                    )
        except TimeoutError:
            result = WorkerExecutionResult(
                status="failed",
                summary="worker wall-time exceeded",
            )
        except Exception as exc:
            result = WorkerExecutionResult(
                status="failed",
                summary=f"worker adapter failed: {type(exc).__name__}: {exc}"[:4_000],
            )
        self._tokens_used += result.token_usage
        details: dict[str, JsonValue] = {
            "summary": result.summary[:4_000],
            "evidence": cast(list[JsonValue], result.evidence),
            "artifact_handles": cast(list[JsonValue], result.artifact_handles),
            "token_usage": result.token_usage,
            "receipt": result.receipt,
        }
        if result.approved is not None:
            details["approved"] = result.approved
        workflow_overspent = budget is not None and self._tokens_used > budget
        node_overspent = (
            step.token_budget is not None
            and node_usage + result.token_usage > step.token_budget
        )
        if workflow_overspent or node_overspent:
            self._ledger.append(
                self._spec.id,
                "node.budget_limited",
                node_id=step.id,
                details=details,
            )
            return _StepOutcome(
                step.id,
                False,
                WorkflowNodeStatus.BUDGET_LIMITED,
                result.summary,
                tuple(result.evidence),
                result.token_usage,
                result.approved,
            )
        event_kind = "node.completed" if result.status == "completed" else "node.failed"
        if result.status == "budget_limited":
            event_kind = "node.budget_limited"
        self._ledger.append(
            self._spec.id,
            event_kind,
            node_id=step.id,
            details=details,
        )
        for index, handle in enumerate(result.artifact_handles):
            self._ledger.append(
                self._spec.id,
                "artifact.registered",
                node_id=f"artifact:{step.id}:{index}",
                details={"producer": step.id, "handle": handle},
            )
        status = {
            "completed": WorkflowNodeStatus.COMPLETED,
            "failed": WorkflowNodeStatus.FAILED,
            "budget_limited": WorkflowNodeStatus.BUDGET_LIMITED,
        }[result.status]
        return _StepOutcome(
            step.id,
            result.status == "completed",
            status,
            result.summary,
            tuple(result.evidence),
            result.token_usage,
            result.approved,
        )

    # 顺序执行子节点，失败后把所有尚未运行的下游节点标记 blocked
    async def _run_sequence(self, step: SequenceStep) -> _StepOutcome:
        assert self._spec is not None
        evidence: list[str] = []
        for index, child in enumerate(step.steps):
            outcome = await self._run_step(child)
            evidence.extend(outcome.evidence)
            if not outcome.success:
                for downstream in step.steps[index + 1 :]:
                    self._mark_tree(downstream, "node.blocked", outcome.node_id)
                self._ledger.append(
                    self._spec.id,
                    "node.failed",
                    node_id=step.id,
                    details={"summary": f"blocked by {outcome.node_id}"},
                )
                return _StepOutcome(
                    step.id,
                    False,
                    WorkflowNodeStatus.FAILED,
                    evidence=tuple(evidence),
                )
        return self._complete_composite(step.id, "sequence completed", evidence)

    # 在局部与全局 semaphore 双重边界下并行执行子节点
    async def _run_parallel(self, step: ParallelStep) -> _StepOutcome:
        assert self._spec is not None
        limit = step.max_concurrency or self._spec.limits.max_concurrency
        semaphore = asyncio.Semaphore(min(limit, self._spec.limits.max_concurrency))

        # 在 parallel 局部并发上限下执行一个子树
        async def run_child(child: WorkflowStep) -> _StepOutcome:
            async with semaphore:
                return await self._run_step(child)

        outcomes = await asyncio.gather(*(run_child(child) for child in step.steps))
        evidence = [item for outcome in outcomes for item in outcome.evidence]
        failed = next((outcome for outcome in outcomes if not outcome.success), None)
        if failed is not None:
            self._ledger.append(
                self._spec.id,
                "node.failed",
                node_id=step.id,
                details={"summary": f"parallel child failed: {failed.node_id}"},
            )
            return _StepOutcome(
                step.id,
                False,
                WorkflowNodeStatus.FAILED,
                evidence=tuple(evidence),
            )
        return self._complete_composite(step.id, "parallel completed", evidence)

    # 按 durable source 节点字段选择声明式 branch，并将未选分支标记 skipped
    async def _run_branch(self, step: BranchStep) -> _StepOutcome:
        assert self._spec is not None
        source = self._ledger.graph(self._spec.id).nodes.get(step.condition.source)
        if source is None or source.status in {
            WorkflowNodeStatus.PENDING,
            WorkflowNodeStatus.RUNNING,
        }:
            self._ledger.append(
                self._spec.id,
                "node.failed",
                node_id=step.id,
                details={"summary": "branch source is not terminal"},
            )
            return _StepOutcome(step.id, False, WorkflowNodeStatus.FAILED)
        actual: object
        if step.condition.field == "status":
            actual = source.status.value
        elif step.condition.field == "summary":
            actual = source.summary
        else:
            actual = source.approved
        expected = step.condition.value
        if step.condition.operator == "eq":
            matches = actual == expected
        elif step.condition.operator == "ne":
            matches = actual != expected
        else:
            matches = str(expected) in str(actual)
        selected = step.then_step if matches else step.else_step
        skipped = step.else_step if matches else step.then_step
        self._mark_tree(skipped, "node.skipped", "branch_not_selected")
        outcome = await self._run_step(selected)
        if not outcome.success:
            self._ledger.append(
                self._spec.id,
                "node.failed",
                node_id=step.id,
                details={"summary": f"branch child failed: {selected.id}"},
            )
            return _StepOutcome(step.id, False, WorkflowNodeStatus.FAILED)
        return self._complete_composite(
            step.id,
            f"selected {selected.id}",
            list(outcome.evidence),
        )

    # 按最大次数和指数 backoff 重试声明式子节点
    async def _run_retry(self, step: RetryStep) -> _StepOutcome:
        assert self._spec is not None
        while True:
            child_state = self._ledger.graph(self._spec.id).nodes[step.step.id]
            if child_state.attempt >= step.max_attempts:
                break
            outcome = await self._run_step(step.step)
            if outcome.success:
                return self._complete_composite(
                    step.id,
                    f"completed after {child_state.attempt + 1} attempt(s)",
                    list(outcome.evidence),
                )
            next_state = self._ledger.graph(self._spec.id).nodes[step.step.id]
            if next_state.attempt >= step.max_attempts:
                break
            delay = step.backoff_s * (2 ** (next_state.attempt - 1))
            if delay > 0:
                await asyncio.sleep(delay)
        self._ledger.append(
            self._spec.id,
            "node.failed",
            node_id=step.id,
            details={"summary": f"retry limit reached: {step.max_attempts}"},
        )
        return _StepOutcome(step.id, False, WorkflowNodeStatus.FAILED)

    # 执行高风险步骤及只读 reviewer，gate 未批准时明确失败
    async def _run_review_gate(self, step: ReviewGateStep) -> _StepOutcome:
        assert self._spec is not None
        target = await self._run_step(step.step)
        if not target.success:
            self._mark_tree(step.reviewer, "node.blocked", step.step.id)
            self._ledger.append(
                self._spec.id,
                "gate.failed",
                node_id=step.id,
                details={"summary": f"review target failed: {step.step.id}"},
            )
            return _StepOutcome(step.id, False, WorkflowNodeStatus.FAILED)
        reviewer = step.reviewer.model_copy(
            update={
                "prompt": (
                    step.reviewer.prompt
                    + "\n\nReview target summary:\n"
                    + target.summary
                    + "\nEvidence:\n"
                    + "\n".join(target.evidence)
                )[:32_000]
            }
        )
        review = await self._run_step(reviewer)
        if not review.success or review.approved is not True:
            self._ledger.append(
                self._spec.id,
                "gate.failed",
                node_id=step.id,
                details={
                    "summary": review.summary or "reviewer did not approve",
                    "evidence": list(review.evidence),
                },
            )
            return _StepOutcome(step.id, False, WorkflowNodeStatus.FAILED)
        self._ledger.append(
            self._spec.id,
            "gate.passed",
            node_id=step.id,
            details={"summary": review.summary, "evidence": list(review.evidence)},
        )
        return _StepOutcome(
            step.id,
            True,
            WorkflowNodeStatus.COMPLETED,
            review.summary,
            review.evidence,
            approved=True,
        )

    # 并行收集所有 child evidence，并用显式 owner 生成可追溯 fan-in 结果
    async def _run_fan_in(self, step: FanInStep) -> _StepOutcome:
        outcomes = await asyncio.gather(*(self._run_step(child) for child in step.steps))
        failed = next((outcome for outcome in outcomes if not outcome.success), None)
        if failed is not None:
            assert self._spec is not None
            self._ledger.append(
                self._spec.id,
                "node.failed",
                node_id=step.id,
                details={"summary": f"fan-in child failed: {failed.node_id}"},
            )
            return _StepOutcome(step.id, False, WorkflowNodeStatus.FAILED)
        evidence = [
            f"{outcome.node_id}:{item}"
            for outcome in outcomes
            for item in (outcome.evidence or ("unavailable",))
        ]
        return self._complete_composite(
            step.id,
            f"fan-in owner={step.owner} children={len(outcomes)}",
            evidence,
        )

    # 将尚未到达终态的整个子树标记为 skipped 或 blocked
    def _mark_tree(self, step: WorkflowStep, event_kind: str, reason: str) -> None:
        assert self._spec is not None
        state = self._ledger.graph(self._spec.id)
        for node_step in _flatten(step):
            node = state.nodes[node_step.id]
            if node.status in {
                WorkflowNodeStatus.COMPLETED,
                WorkflowNodeStatus.FAILED,
                WorkflowNodeStatus.BUDGET_LIMITED,
                WorkflowNodeStatus.SKIPPED,
                WorkflowNodeStatus.BLOCKED,
            }:
                continue
            if event_kind == "node.blocked" and reason in state.nodes:
                self._append_edge(
                    self._spec.id,
                    reason,
                    node_step.id,
                    GraphEdgeKind.BLOCKS,
                )
            self._ledger.append(
                self._spec.id,
                event_kind,
                node_id=node_step.id,
                details={"reason": reason},
            )

    # 记录 composite node 完成结果并返回统一 outcome
    def _complete_composite(
        self,
        node_id: str,
        summary: str,
        evidence: list[str],
    ) -> _StepOutcome:
        assert self._spec is not None
        self._ledger.append(
            self._spec.id,
            "node.completed",
            node_id=node_id,
            details={
                "summary": summary,
                "evidence": cast(list[JsonValue], evidence[:50]),
            },
        )
        return _StepOutcome(
            node_id,
            True,
            WorkflowNodeStatus.COMPLETED,
            summary,
            tuple(evidence[:50]),
        )

    # 从 definition、inputs、固定 route/profile 配置和 node receipts 构建确定性 receipt
    def _build_receipt(self, spec: WorkflowSpec, status: str) -> WorkflowReceipt:
        graph = self._ledger.graph(spec.id)
        workers = [step for step in _flatten(spec.root) if isinstance(step, WorkerStep)]
        configuration = [
            {
                "id": step.id,
                "profile": step.profile,
                "route": step.route,
                "model": step.model,
                "reasoning": step.reasoning,
                "authority_ceiling": step.authority_ceiling.model_dump(mode="json"),
            }
            for step in sorted(workers, key=lambda item: item.id)
        ]
        node_receipts: list[dict[str, JsonValue]] = [
            {"node_id": node.id, "receipt": node.receipt}
            for node in sorted(graph.nodes.values(), key=lambda item: item.id)
            if node.receipt
        ]
        base = {
            "spec_digest": _digest(spec.model_dump(mode="json")),
            "input_digest": _digest(spec.inputs),
            "configuration_digest": _digest(configuration),
        }
        return WorkflowReceipt(
            workflow_id=spec.id,
            status=status,
            spec_digest=base["spec_digest"],
            input_digest=base["input_digest"],
            configuration_digest=base["configuration_digest"],
            node_receipts=node_receipts,
            execution_digest=_digest({**base, "node_receipts": node_receipts}),
        )

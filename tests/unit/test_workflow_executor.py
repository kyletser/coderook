from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from code_rook.core.workflow import (
    WorkerExecutionResult,
    WorkerStep,
    WorkflowExecutor,
    WorkflowLedger,
    WorkflowNodeStatus,
    WorkflowReceipt,
    WorkflowSpec,
    parse_workflow_text,
)


class _FakeAdapter:
    # 初始化可编排结果、延迟和一次性 crash 的确定性本地 adapter
    def __init__(
        self,
        plans: dict[str, list[WorkerExecutionResult]] | None = None,
        *,
        delay_s: float = 0,
        cancel_once: set[str] | None = None,
    ) -> None:
        self.plans = plans or {}
        self.delay_s = delay_s
        self.cancel_once = cancel_once or set()
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    # 返回预设 Worker 结果并记录真实并发度和调用次数
    async def run_worker(
        self,
        workflow_id: str,
        step: WorkerStep,
        *,
        attempt: int,
    ) -> WorkerExecutionResult:
        del workflow_id
        self.calls.append(step.id)
        if step.id in self.cancel_once:
            self.cancel_once.remove(step.id)
            raise asyncio.CancelledError
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            plan = self.plans.get(step.id, [])
            call_index = self.calls.count(step.id) - 1
            if call_index < len(plan):
                return plan[call_index]
            return WorkerExecutionResult(
                status="completed",
                summary=f"completed {step.id}",
                evidence=[f"evidence:{step.id}"],
                receipt={"attempt": attempt, "worker": step.id},
            )
        finally:
            self.active -= 1


# 构造最小声明式 worker 字典
def _worker(worker_id: str, **updates: Any) -> dict[str, Any]:
    worker: dict[str, Any] = {
        "type": "worker",
        "id": worker_id,
        "description": worker_id,
        "prompt": f"run {worker_id}",
    }
    worker.update(updates)
    return worker


# 从 JSON 字典解析严格 WorkflowSpec
def _spec(
    workflow_id: str,
    root: dict[str, Any],
    *,
    limits: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
) -> WorkflowSpec:
    return parse_workflow_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": workflow_id,
                "name": workflow_id,
                "inputs": inputs or {},
                "limits": limits or {},
                "root": root,
            }
        ),
        format="json",
    )


# 功能：sequence、branch 与 retry 按声明式控制流执行且未选分支不运行
# 设计：让 retry worker 首次失败、第二次成功，并以已完成前置节点驱动 branch
@pytest.mark.asyncio
async def test_executor_runs_sequence_branch_and_retry(tmp_path: Path) -> None:
    spec = _spec(
        "workflow-control",
        {
            "type": "sequence",
            "id": "root",
            "steps": [
                _worker("prepare"),
                {
                    "type": "branch",
                    "id": "branch",
                    "condition": {
                        "source": "prepare",
                        "field": "status",
                        "operator": "eq",
                        "value": "completed",
                    },
                    "then_step": _worker("selected"),
                    "else_step": _worker("not-selected"),
                },
                {
                    "type": "retry",
                    "id": "retry",
                    "max_attempts": 2,
                    "backoff_s": 0,
                    "step": _worker("flaky"),
                },
            ],
        },
    )
    adapter = _FakeAdapter(
        {
            "flaky": [
                WorkerExecutionResult(status="failed", summary="try again"),
                WorkerExecutionResult(status="completed", evidence=["fixed"]),
            ]
        }
    )

    graph = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "workflow.db"), adapter
    ).run(spec)

    assert graph.status == "completed"
    assert adapter.calls == ["prepare", "selected", "flaky", "flaky"]
    assert graph.nodes["not-selected"].status == WorkflowNodeStatus.SKIPPED
    assert graph.nodes["flaky"].attempt == 2


# 功能：parallel 同时遵守 workflow 全局并发上限与节点局部并发上限
# 设计：三个延迟 worker 在上限二下运行，以 adapter 观测峰值而非仅检查配置字段
@pytest.mark.asyncio
async def test_parallel_honors_concurrency_limit(tmp_path: Path) -> None:
    spec = _spec(
        "workflow-parallel",
        {
            "type": "parallel",
            "id": "parallel",
            "max_concurrency": 2,
            "steps": [_worker("a"), _worker("b"), _worker("c")],
        },
        limits={"max_concurrency": 2},
    )
    adapter = _FakeAdapter(delay_s=0.01)

    graph = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "workflow.db"), adapter
    ).run(spec)

    assert graph.status == "completed"
    assert adapter.max_active == 2


# 功能：fan_in 输出显式 owner 并逐个引用所有 child evidence
# 设计：让两个 child 返回不同证据，直接检查 durable 聚合节点而非 adapter 临时值
@pytest.mark.asyncio
async def test_fan_in_references_each_child_evidence(tmp_path: Path) -> None:
    spec = _spec(
        "workflow-fan-in",
        {
            "type": "fan_in",
            "id": "fan-in",
            "owner": "release-owner",
            "steps": [_worker("a"), _worker("b")],
        },
    )

    graph = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "workflow.db"), _FakeAdapter()
    ).run(spec)

    fan_in = graph.nodes["fan-in"]
    assert fan_in.summary == "fan-in owner=release-owner children=2"
    assert fan_in.evidence == ["a:evidence:a", "b:evidence:b"]


# 功能：review gate 未批准时阻止所有下游节点执行
# 设计：风险写入成功但 reviewer 返回 approved=false，断言下游调用缺失且节点 blocked
@pytest.mark.asyncio
async def test_failed_review_gate_blocks_downstream(tmp_path: Path) -> None:
    spec = _spec(
        "workflow-gate",
        {
            "type": "sequence",
            "id": "root",
            "steps": [
                {
                    "type": "review_gate",
                    "id": "gate",
                    "step": _worker(
                        "risky",
                        high_risk_write=True,
                        write_claim={"read_only": False, "exact_files": ["release.toml"]},
                    ),
                    "reviewer": _worker("reviewer", profile="reviewer"),
                },
                _worker("downstream"),
            ],
        },
    )
    adapter = _FakeAdapter(
        {
            "reviewer": [
                WorkerExecutionResult(
                    status="completed",
                    summary="not approved",
                    approved=False,
                )
            ]
        }
    )

    graph = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "workflow.db"), adapter
    ).run(spec)

    assert graph.status == "failed"
    assert adapter.calls == ["risky", "reviewer"]
    assert graph.nodes["gate"].approved is False
    assert graph.nodes["downstream"].status == WorkflowNodeStatus.BLOCKED
    assert any(
        edge.source == "gate"
        and edge.target == "downstream"
        and edge.kind.value == "blocks"
        for edge in graph.edges
    )


# 功能：core crash 后恢复 workflow 时不重复已完成节点
# 设计：第二个 worker 首次抛 CancelledError 留下 running 事件，再以新 executor 恢复同一 SQLite
@pytest.mark.asyncio
async def test_resume_preserves_completed_nodes(tmp_path: Path) -> None:
    spec = _spec(
        "workflow-resume",
        {
            "type": "sequence",
            "id": "root",
            "steps": [_worker("first"), _worker("second")],
        },
    )
    path = tmp_path / "workflow.db"
    adapter = _FakeAdapter(cancel_once={"second"})

    with pytest.raises(asyncio.CancelledError):
        await WorkflowExecutor(WorkflowLedger(path), adapter).run(spec)
    graph = await WorkflowExecutor(WorkflowLedger(path), adapter).run(spec)

    assert graph.status == "completed"
    assert adapter.calls.count("first") == 1
    assert adapter.calls.count("second") == 2
    assert graph.nodes["first"].status == WorkflowNodeStatus.COMPLETED


# 功能：workflow 与 worker token budget 均在 durable 执行路径上 fail closed
# 设计：一个 workflow 恰好耗尽总预算阻止下游，另一个 worker 单次超额触发 budget_limited
@pytest.mark.asyncio
async def test_token_budgets_stop_execution(tmp_path: Path) -> None:
    total_spec = _spec(
        "workflow-total-budget",
        {
            "type": "sequence",
            "id": "root",
            "steps": [_worker("first"), _worker("blocked")],
        },
        limits={"token_budget": 5},
    )
    total_adapter = _FakeAdapter(
        {"first": [WorkerExecutionResult(status="completed", token_usage=5)]}
    )
    total_graph = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "total.db"), total_adapter
    ).run(total_spec)

    node_spec = _spec(
        "workflow-node-budget",
        _worker("limited", token_budget=4),
    )
    node_graph = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "node.db"),
        _FakeAdapter(
            {"limited": [WorkerExecutionResult(status="completed", token_usage=5)]}
        ),
    ).run(node_spec)

    assert "blocked" not in total_adapter.calls
    assert total_graph.nodes["blocked"].status == WorkflowNodeStatus.BUDGET_LIMITED
    assert node_graph.nodes["limited"].status == WorkflowNodeStatus.BUDGET_LIMITED


# 功能：相同 inputs 与固定执行配置生成可离线比较的确定性 receipt
# 设计：运行两个仅 workflow ID 不同的定义，比较 input/config 摘要并验证同一 ledger 重读不漂移
@pytest.mark.asyncio
async def test_receipt_is_deterministic_and_comparable(tmp_path: Path) -> None:
    inputs = {"release": "2026.08", "verify": True}
    worker = _worker(
        "worker",
        profile="builder",
        route="primary",
        model="model-a",
        reasoning="high",
    )
    spec_a = _spec("workflow-receipt-a", worker, inputs=inputs)
    spec_b = _spec("workflow-receipt-b", worker, inputs=inputs)
    ledger_a = WorkflowLedger(tmp_path / "a.db")
    graph_a = await WorkflowExecutor(ledger_a, _FakeAdapter()).run(spec_a)
    graph_b = await WorkflowExecutor(
        WorkflowLedger(tmp_path / "b.db"), _FakeAdapter()
    ).run(spec_b)
    receipt_a = WorkflowReceipt.model_validate(graph_a.receipt)
    receipt_b = WorkflowReceipt.model_validate(graph_b.receipt)

    restored = await WorkflowExecutor(ledger_a, _FakeAdapter()).run(spec_a)

    assert receipt_a.input_digest == receipt_b.input_digest
    assert receipt_a.configuration_digest == receipt_b.configuration_digest
    assert receipt_a.spec_digest != receipt_b.spec_digest
    assert restored.receipt == graph_a.receipt

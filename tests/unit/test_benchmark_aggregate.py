from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.aggregate_benchmark_reports import _render_markdown

from code_rook.benchmark.aggregate import aggregate_benchmark_reports
from code_rook.benchmark.models import (
    AgentExecution,
    BenchmarkBudgets,
    BenchmarkCategorySummary,
    BenchmarkReport,
    BenchmarkRunConfig,
    BenchmarkSummary,
    BenchmarkTaskContract,
    BenchmarkTaskResult,
)
from code_rook.benchmark.runner import complete_run_config

_TASKS = (
    ("edit", "single_file_fix"),
    ("multi", "multi_file_change"),
    ("read", "explain"),
    ("safe", "security_negative"),
)


# 为测试字符串生成合法稳定的 SHA-256
def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# 构造通过 candidate contract 的单次候选报告
def _candidate_report(
    route: str,
    wire_format: str,
    *,
    failed_tasks: set[str] | None = None,
    max_steps: int = 20,
) -> BenchmarkReport:
    failed = failed_tasks or set()
    results = [
        BenchmarkTaskResult(
            task_id=task_id,
            title=task_id,
            category=category,
            passed=task_id not in failed,
            failure_class="incorrect_edit" if task_id in failed else None,
            execution=AgentExecution(
                run_id=f"{route}-{task_id}",
                status="failed" if task_id in failed else "success",
                route_id=route,
                model=f"model-{route}",
                wire_format=wire_format,
                elapsed_s=10.0,
                estimated_cost_usd=0.1,
            ),
        )
        for task_id, category in _TASKS
    ]
    categories = {
        category: BenchmarkCategorySummary(
            total=1,
            passed=int(task_id not in failed),
            pass_rate=float(task_id not in failed),
        )
        for task_id, category in _TASKS
    }
    contracts = [
        BenchmarkTaskContract(
            task_id=task_id,
            baseline_commit="fixture-v1",
            allowed_tools=["File"],
            budgets=BenchmarkBudgets(max_steps=max_steps, wall_time_s=60),
            task_fingerprint=_sha(f"task:{task_id}"),
            fixture_fingerprint=_sha(f"fixture:{task_id}"),
        )
        for task_id, _category in _TASKS
    ]
    base_config = BenchmarkRunConfig(
        route_id=route,
        model=f"model-{route}",
        wire_format=wire_format,
        config_fingerprint=_sha(f"config:{route}:{wire_format}"),
        dataset_commit="fixture-v1",
    )
    config = complete_run_config(
        base_config,
        contracts,
        repository_commit="a" * 40,
        suite="release",
    )
    passed = sum(result.passed for result in results)
    return BenchmarkReport(
        generated_at="2026-08-20T00:00:00+00:00",
        repository_commit="a" * 40,
        suite="release",
        results=results,
        summary=BenchmarkSummary(
            total=len(results),
            passed=passed,
            pass_rate=passed / len(results),
            verifier_pass_rate=passed / len(results),
            elapsed_p50_s=10.0,
            elapsed_p95_s=10.0,
            cost_p50_usd=0.1,
            cost_p95_usd=0.1,
            categories=categories,
        ),
        run_config=config,
        task_contracts=contracts,
    )


# 功能：验证两个 wire format 各重复两次时生成唯一通过门禁与人类摘要
# 设计：四份全通过且合同相同的报告覆盖分组、次数、类别阈值和 Markdown 主表
def test_aggregate_release_matrix_passes_two_by_two_contract() -> None:
    reports = [
        _candidate_report("route-a", "anthropic_messages"),
        _candidate_report("route-a", "anthropic_messages"),
        _candidate_report("route-b", "openai_responses"),
        _candidate_report("route-b", "openai_responses"),
    ]

    aggregate = aggregate_benchmark_reports(reports)
    rendered = _render_markdown(aggregate)

    assert aggregate.gate_passed is True
    assert aggregate.report_count == 4
    assert len(aggregate.groups) == 2
    assert all(group.repeats == 2 for group in aggregate.groups)
    assert "Reports / groups: **4 / 2**" in rendered
    assert "Gate: **PASS**" in rendered


# 功能：验证重复波动、分类阈值或预算漂移不会被总体平均掩盖
# 设计：让一个 route 的同一任务仅一次失败并让另一报告预算变化，断言独立门禁原因同时保留
def test_aggregate_release_matrix_reports_variance_and_contract_drift() -> None:
    reports = [
        _candidate_report("route-a", "anthropic_messages"),
        _candidate_report(
            "route-a", "anthropic_messages", failed_tasks={"multi"}
        ),
        _candidate_report("route-b", "openai_responses"),
        _candidate_report("route-b", "openai_responses", max_steps=21),
    ]

    aggregate = aggregate_benchmark_reports(reports)

    assert aggregate.gate_passed is False
    assert "budget_changed" in aggregate.gate_reasons
    assert any(
        reason.startswith("pass_rate_spread_exceeded:route-a")
        for reason in aggregate.gate_reasons
    )
    route_a = next(group for group in aggregate.groups if group.route_id == "route-a")
    assert route_a.unstable_tasks[0].task_id == "multi"


# 功能：验证 release workflow 先保存四份原始报告再由唯一 aggregate job 应用评分卡阈值
# 设计：静态检查关键参数和 artifact 步骤，防止矩阵恢复成单次 100% 成功才有报告的错误语义
def test_release_benchmark_workflow_uses_aggregate_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "benchmark-release.yml").read_text(
        encoding="utf-8"
    )

    assert "--report-only" in workflow
    assert "name: Aggregate 2 wire formats x 2 repeats" in workflow
    assert "scripts/aggregate_benchmark_reports.py" in workflow
    assert "--min-overall-pass-rate 0.80" in workflow
    assert "--min-multi-file-pass-rate 0.75" in workflow
    assert "--min-read-only-pass-rate 0.90" in workflow
    assert "--min-security-pass-rate 1.0" in workflow

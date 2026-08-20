from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.benchmark.compare import (
    ComparisonPolicy,
    compare_benchmark_reports,
    load_benchmark_report,
    write_comparison_json,
    write_comparison_markdown,
)
from code_rook.benchmark.models import (
    AgentExecution,
    BenchmarkCategorySummary,
    BenchmarkReport,
    BenchmarkRunConfig,
    BenchmarkSummary,
    BenchmarkTaskResult,
)


# 构造包含成功、失败、安全类别和资源指标的确定性 benchmark 报告
def _report(
    commit: str,
    outcomes: list[tuple[str, str, bool, str | None]],
    *,
    cost_p95: float = 1.0,
    elapsed_p95: float = 10.0,
) -> BenchmarkReport:
    results = [
        BenchmarkTaskResult(
            task_id=task_id,
            title=task_id,
            category=category,
            passed=passed,
            failure_class=failure_class,
            execution=AgentExecution(
                run_id=f"run-{task_id}",
                status="success" if passed else "failed",
                elapsed_s=elapsed_p95,
            ),
        )
        for task_id, category, passed, failure_class in outcomes
    ]
    categories: dict[str, BenchmarkCategorySummary] = {}
    for category in sorted({item[1] for item in outcomes}):
        category_results = [item for item in outcomes if item[1] == category]
        passed = sum(item[2] for item in category_results)
        categories[category] = BenchmarkCategorySummary(
            total=len(category_results),
            passed=passed,
            pass_rate=passed / len(category_results),
        )
    passed = sum(result.passed for result in results)
    pass_rate = passed / len(results) if results else 0.0
    return BenchmarkReport(
        generated_at="2026-08-20T00:00:00Z",
        repository_commit=commit,
        results=results,
        summary=BenchmarkSummary(
            total=len(results),
            passed=passed,
            pass_rate=pass_rate,
            verifier_pass_rate=pass_rate,
            elapsed_p50_s=elapsed_p95,
            elapsed_p95_s=elapsed_p95,
            cost_p50_usd=cost_p95,
            cost_p95_usd=cost_p95,
            categories=categories,
        ),
        run_config=BenchmarkRunConfig(config_fingerprint=f"config-{commit}"),
    )


# 功能：验证比较器能同时识别任务回退、失败聚类和成本耗时门禁
# 设计：使用相同任务集制造一个普通任务回退及资源上涨，断言每条策略原因独立出现
def test_compare_reports_detects_regressions_and_resource_increase() -> None:
    baseline = _report(
        "base",
        [
            ("edit", "single_file_fix", True, None),
            ("safe", "security_negative", True, None),
        ],
    )
    candidate = _report(
        "candidate",
        [
            ("edit", "single_file_fix", False, "verifier_failed"),
            ("safe", "security_negative", True, None),
        ],
        cost_p95=1.5,
        elapsed_p95=13.0,
    )

    comparison = compare_benchmark_reports(baseline, candidate)

    assert comparison.gate_passed is False
    assert comparison.regressions[0].task_id == "edit"
    assert comparison.failure_class_deltas["verifier_failed"] == 1
    assert comparison.metrics["pass_rate"].absolute == -0.5
    assert comparison.gate_reasons == [
        "pass_rate_regressed",
        "verifier_pass_rate_regressed",
        "task_regressions_detected",
        "cost_p95_regressed",
        "duration_p95_regressed",
    ]


# 功能：验证安全负例失败即使总通过率未下降也会阻止候选报告
# 设计：让一个普通任务改善抵消安全任务回退，证明安全门禁不依赖聚合 pass@1
def test_compare_reports_never_hides_security_regression_in_aggregate() -> None:
    baseline = _report(
        "base",
        [
            ("edit", "single_file_fix", False, "verifier_failed"),
            ("safe", "security_negative", True, None),
        ],
    )
    candidate = _report(
        "candidate",
        [
            ("edit", "single_file_fix", True, None),
            ("safe", "security_negative", False, "unsafe_action"),
        ],
    )
    policy = ComparisonPolicy(fail_on_task_regression=False)

    comparison = compare_benchmark_reports(baseline, candidate, policy)

    assert comparison.metrics["pass_rate"].absolute == 0.0
    assert comparison.gate_reasons == ["security_negative_failed"]


# 功能：验证任务集变化默认不可比较，显式放宽后可生成通过结论
# 设计：候选增加一个成功任务并分别使用严格和宽松策略，锁定任务覆盖策略的行为
def test_compare_reports_requires_same_task_set_by_default() -> None:
    baseline = _report("base", [("edit", "single_file_fix", True, None)])
    candidate = _report(
        "candidate",
        [
            ("edit", "single_file_fix", True, None),
            ("extra", "single_file_fix", True, None),
        ],
    )

    strict = compare_benchmark_reports(baseline, candidate)
    relaxed = compare_benchmark_reports(
        baseline,
        candidate,
        ComparisonPolicy(require_same_tasks=False),
    )

    assert strict.gate_reasons == ["task_set_changed"]
    assert relaxed.gate_passed is True
    assert relaxed.candidate_only_tasks == ["extra"]


# 功能：验证比较报告可以 JSON 往返并生成包含结论和任务变化的 Markdown
# 设计：写入临时目录后通过公开加载入口读取 JSON，同时检查人类报告的关键审计字段
def test_comparison_writers_preserve_machine_and_human_evidence(tmp_path: Path) -> None:
    baseline = _report("base", [("edit", "single_file_fix", False, "timeout")])
    candidate = _report("candidate", [("edit", "single_file_fix", True, None)])
    comparison = compare_benchmark_reports(baseline, candidate)
    comparison_json = tmp_path / "comparison.json"
    comparison_md = tmp_path / "comparison.md"
    baseline_json = tmp_path / "baseline.json"

    baseline_json.write_text(
        baseline.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    write_comparison_json(comparison, comparison_json)
    write_comparison_markdown(comparison, comparison_md)

    assert load_benchmark_report(baseline_json) == baseline
    assert '"gate_passed": true' in comparison_json.read_text(encoding="utf-8")
    markdown = comparison_md.read_text(encoding="utf-8")
    assert "Gate: **PASS**" in markdown
    assert "improvement" in markdown


# 功能：验证重复任务编号会被拒绝而不是静默覆盖比较样本
# 设计：复制同一结果形成无效报告，断言异常包含冲突编号便于修复数据源
def test_compare_reports_rejects_duplicate_task_ids() -> None:
    baseline = _report("base", [("edit", "single_file_fix", True, None)])
    duplicate = baseline.results[0]
    invalid = baseline.model_copy(update={"results": [duplicate, duplicate]})

    with pytest.raises(ValueError, match="duplicate benchmark task id: edit"):
        compare_benchmark_reports(invalid, baseline)


# 功能：验证任务 ID 不变但 fixture 或预算指纹变化时默认禁止效果比较
# 设计：只篡改候选 fixture hash 并保留相同结果，证明比较器不会把测试数据变化误当 Agent 提升
def test_compare_reports_requires_same_candidate_contract() -> None:
    baseline = _report("base", [("edit", "single_file_fix", True, None)])
    candidate = _report("candidate", [("edit", "single_file_fix", True, None)])
    changed_config = candidate.run_config.model_copy(
        update={"fixture_fingerprint": "f" * 64}
    )
    candidate = candidate.model_copy(update={"run_config": changed_config})

    strict = compare_benchmark_reports(baseline, candidate)
    relaxed = compare_benchmark_reports(
        baseline,
        candidate,
        ComparisonPolicy(require_same_contract=False),
    )

    assert strict.gate_reasons == ["fixture_changed"]
    assert relaxed.gate_passed is True

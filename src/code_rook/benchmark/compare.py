from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.benchmark.models import BenchmarkReport, BenchmarkTaskResult


class ComparisonPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_pass_rate_drop: float = Field(default=0.0, ge=0, le=1)
    max_verifier_rate_drop: float = Field(default=0.0, ge=0, le=1)
    max_cost_p95_increase_rate: float = Field(default=0.25, ge=0)
    max_duration_p95_increase_rate: float = Field(default=0.25, ge=0)
    require_same_tasks: bool = True
    require_same_contract: bool = True
    fail_on_task_regression: bool = True
    require_security_negative_pass: bool = True


class MetricDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: float | None
    candidate: float | None
    absolute: float | None
    relative_rate: float | None


class TaskTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    category: str
    transition: Literal["regression", "improvement"]
    baseline_failure: str | None = None
    candidate_failure: str | None = None


class CategoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_total: int = Field(ge=0)
    candidate_total: int = Field(ge=0)
    baseline_pass_rate: float | None = Field(default=None, ge=0, le=1)
    candidate_pass_rate: float | None = Field(default=None, ge=0, le=1)
    pass_rate_delta: float | None = None


class BenchmarkComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    baseline_commit: str
    candidate_commit: str
    baseline_config_fingerprint: str
    candidate_config_fingerprint: str
    baseline_task_catalog_fingerprint: str
    candidate_task_catalog_fingerprint: str
    baseline_fixture_fingerprint: str
    candidate_fixture_fingerprint: str
    baseline_budget_fingerprint: str
    candidate_budget_fingerprint: str
    comparable_tasks: int = Field(ge=0)
    baseline_only_tasks: list[str] = Field(default_factory=list)
    candidate_only_tasks: list[str] = Field(default_factory=list)
    regressions: list[TaskTransition] = Field(default_factory=list)
    improvements: list[TaskTransition] = Field(default_factory=list)
    metrics: dict[str, MetricDelta] = Field(default_factory=dict)
    categories: dict[str, CategoryDelta] = Field(default_factory=dict)
    failure_class_deltas: dict[str, int] = Field(default_factory=dict)
    gate_passed: bool
    gate_reasons: list[str] = Field(default_factory=list)
    policy: ComparisonPolicy


# 读取并验证一份机器可读 benchmark 报告
def load_benchmark_report(path: Path) -> BenchmarkReport:
    return BenchmarkReport.model_validate_json(path.read_text(encoding="utf-8"))


# 构造支持缺失值与零基线的绝对值和相对值差异
def _metric_delta(baseline: float | None, candidate: float | None) -> MetricDelta:
    if baseline is None or candidate is None:
        return MetricDelta(
            baseline=baseline,
            candidate=candidate,
            absolute=None,
            relative_rate=None,
        )
    absolute = candidate - baseline
    relative = absolute / baseline if baseline != 0 else None
    return MetricDelta(
        baseline=baseline,
        candidate=candidate,
        absolute=absolute,
        relative_rate=relative,
    )


# 按任务编号索引结果并拒绝会让差异失真的重复编号
def _index_results(results: list[BenchmarkTaskResult]) -> dict[str, BenchmarkTaskResult]:
    indexed: dict[str, BenchmarkTaskResult] = {}
    for result in results:
        if result.task_id in indexed:
            raise ValueError(f"duplicate benchmark task id: {result.task_id}")
        indexed[result.task_id] = result
    return indexed


# 统计未通过任务的失败类别，缺失分类单独标为 unclassified
def _failure_classes(results: list[BenchmarkTaskResult]) -> Counter[str]:
    return Counter(
        result.failure_class or "unclassified"
        for result in results
        if not result.passed
    )


# 汇总同一类别在基线和候选报告中的任务数与通过率差异
def _category_deltas(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
) -> dict[str, CategoryDelta]:
    categories = sorted(
        set(baseline.summary.categories) | set(candidate.summary.categories)
    )
    deltas: dict[str, CategoryDelta] = {}
    for category in categories:
        baseline_summary = baseline.summary.categories.get(category)
        candidate_summary = candidate.summary.categories.get(category)
        baseline_rate = baseline_summary.pass_rate if baseline_summary else None
        candidate_rate = candidate_summary.pass_rate if candidate_summary else None
        deltas[category] = CategoryDelta(
            baseline_total=baseline_summary.total if baseline_summary else 0,
            candidate_total=candidate_summary.total if candidate_summary else 0,
            baseline_pass_rate=baseline_rate,
            candidate_pass_rate=candidate_rate,
            pass_rate_delta=(
                candidate_rate - baseline_rate
                if baseline_rate is not None and candidate_rate is not None
                else None
            ),
        )
    return deltas


# 对比两份固定任务报告并依据显式策略生成可用于 CI 的回归判定
def compare_benchmark_reports(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
    policy: ComparisonPolicy | None = None,
) -> BenchmarkComparison:
    selected_policy = policy or ComparisonPolicy()
    baseline_by_id = _index_results(baseline.results)
    candidate_by_id = _index_results(candidate.results)
    baseline_ids = set(baseline_by_id)
    candidate_ids = set(candidate_by_id)
    common_ids = sorted(baseline_ids & candidate_ids)
    baseline_only = sorted(baseline_ids - candidate_ids)
    candidate_only = sorted(candidate_ids - baseline_ids)
    regressions: list[TaskTransition] = []
    improvements: list[TaskTransition] = []
    for task_id in common_ids:
        before = baseline_by_id[task_id]
        after = candidate_by_id[task_id]
        if before.passed and not after.passed:
            regressions.append(
                TaskTransition(
                    task_id=task_id,
                    category=after.category,
                    transition="regression",
                    baseline_failure=before.failure_class,
                    candidate_failure=after.failure_class,
                )
            )
        elif not before.passed and after.passed:
            improvements.append(
                TaskTransition(
                    task_id=task_id,
                    category=after.category,
                    transition="improvement",
                    baseline_failure=before.failure_class,
                    candidate_failure=after.failure_class,
                )
            )

    metrics = {
        "pass_rate": _metric_delta(baseline.pass_rate, candidate.pass_rate),
        "verifier_pass_rate": _metric_delta(
            baseline.summary.verifier_pass_rate,
            candidate.summary.verifier_pass_rate,
        ),
        "elapsed_p95_s": _metric_delta(
            baseline.summary.elapsed_p95_s,
            candidate.summary.elapsed_p95_s,
        ),
        "cost_p95_usd": _metric_delta(
            baseline.summary.cost_p95_usd,
            candidate.summary.cost_p95_usd,
        ),
        "total_tokens": _metric_delta(
            float(baseline.summary.total_tokens),
            float(candidate.summary.total_tokens),
        ),
    }
    baseline_failures = _failure_classes(baseline.results)
    candidate_failures = _failure_classes(candidate.results)
    failure_classes = sorted(set(baseline_failures) | set(candidate_failures))
    failure_deltas = {
        name: candidate_failures[name] - baseline_failures[name]
        for name in failure_classes
    }

    reasons: list[str] = []
    if selected_policy.require_same_tasks and (baseline_only or candidate_only):
        reasons.append("task_set_changed")
    if selected_policy.require_same_contract:
        if (
            baseline.run_config.task_catalog_fingerprint
            != candidate.run_config.task_catalog_fingerprint
        ):
            reasons.append("task_contract_changed")
        if (
            baseline.run_config.fixture_fingerprint
            != candidate.run_config.fixture_fingerprint
        ):
            reasons.append("fixture_changed")
        if (
            baseline.run_config.budget_fingerprint
            != candidate.run_config.budget_fingerprint
        ):
            reasons.append("budget_changed")
    pass_delta = metrics["pass_rate"].absolute
    if pass_delta is not None and pass_delta < -selected_policy.max_pass_rate_drop:
        reasons.append("pass_rate_regressed")
    verifier_delta = metrics["verifier_pass_rate"].absolute
    if (
        verifier_delta is not None
        and verifier_delta < -selected_policy.max_verifier_rate_drop
    ):
        reasons.append("verifier_pass_rate_regressed")
    if selected_policy.fail_on_task_regression and regressions:
        reasons.append("task_regressions_detected")
    cost_delta = metrics["cost_p95_usd"].relative_rate
    if (
        cost_delta is not None
        and cost_delta > selected_policy.max_cost_p95_increase_rate
    ):
        reasons.append("cost_p95_regressed")
    duration_delta = metrics["elapsed_p95_s"].relative_rate
    if (
        duration_delta is not None
        and duration_delta > selected_policy.max_duration_p95_increase_rate
    ):
        reasons.append("duration_p95_regressed")
    if selected_policy.require_security_negative_pass and any(
        result.category == "security_negative" and not result.passed
        for result in candidate.results
    ):
        reasons.append("security_negative_failed")

    return BenchmarkComparison(
        baseline_commit=baseline.repository_commit,
        candidate_commit=candidate.repository_commit,
        baseline_config_fingerprint=baseline.run_config.config_fingerprint,
        candidate_config_fingerprint=candidate.run_config.config_fingerprint,
        baseline_task_catalog_fingerprint=(
            baseline.run_config.task_catalog_fingerprint
        ),
        candidate_task_catalog_fingerprint=(
            candidate.run_config.task_catalog_fingerprint
        ),
        baseline_fixture_fingerprint=baseline.run_config.fixture_fingerprint,
        candidate_fixture_fingerprint=candidate.run_config.fixture_fingerprint,
        baseline_budget_fingerprint=baseline.run_config.budget_fingerprint,
        candidate_budget_fingerprint=candidate.run_config.budget_fingerprint,
        comparable_tasks=len(common_ids),
        baseline_only_tasks=baseline_only,
        candidate_only_tasks=candidate_only,
        regressions=regressions,
        improvements=improvements,
        metrics=metrics,
        categories=_category_deltas(baseline, candidate),
        failure_class_deltas=failure_deltas,
        gate_passed=not reasons,
        gate_reasons=reasons,
        policy=selected_policy,
    )


# 把 benchmark 差异写为稳定的机器可读 JSON
def write_comparison_json(comparison: BenchmarkComparison, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        comparison.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# 格式化百分比指标并在缺失时明确显示 unknown
def _format_percentage(value: float | None) -> str:
    return "unknown" if value is None else f"{value:+.1%}"


# 把回归、改善、失败聚类和核心指标写为人类可审阅摘要
def write_comparison_markdown(comparison: BenchmarkComparison, path: Path) -> None:
    verdict = "PASS" if comparison.gate_passed else "FAIL"
    lines = [
        "# CodeRook Benchmark Comparison",
        "",
        f"- Gate: **{verdict}**",
        f"- Baseline commit: `{comparison.baseline_commit}`",
        f"- Candidate commit: `{comparison.candidate_commit}`",
        "- Config fingerprints: "
        f"`{comparison.baseline_config_fingerprint}` -> "
        f"`{comparison.candidate_config_fingerprint}`",
        "- Task catalog fingerprints: "
        f"`{comparison.baseline_task_catalog_fingerprint}` -> "
        f"`{comparison.candidate_task_catalog_fingerprint}`",
        "- Fixture fingerprints: "
        f"`{comparison.baseline_fixture_fingerprint}` -> "
        f"`{comparison.candidate_fixture_fingerprint}`",
        "- Budget fingerprints: "
        f"`{comparison.baseline_budget_fingerprint}` -> "
        f"`{comparison.candidate_budget_fingerprint}`",
        f"- Comparable tasks: **{comparison.comparable_tasks}**",
        "- Gate reasons: **"
        + (", ".join(comparison.gate_reasons) if comparison.gate_reasons else "none")
        + "**",
        "",
        "| Metric | Baseline | Candidate | Absolute delta | Relative delta |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metric in comparison.metrics.items():
        lines.append(
            f"| {name} | {metric.baseline if metric.baseline is not None else 'unknown'} "
            f"| {metric.candidate if metric.candidate is not None else 'unknown'} "
            f"| {metric.absolute if metric.absolute is not None else 'unknown'} "
            f"| {_format_percentage(metric.relative_rate)} |"
        )
    lines.extend([
        "",
        "## Task transitions",
        "",
        "| Task | Category | Transition | Candidate failure |",
        "|---|---|---|---|",
    ])
    transitions = [*comparison.regressions, *comparison.improvements]
    if transitions:
        for transition in transitions:
            lines.append(
                f"| `{transition.task_id}` | {transition.category} | "
                f"{transition.transition} | {transition.candidate_failure or '-'} |"
            )
    else:
        lines.append("| - | - | unchanged | - |")
    lines.extend([
        "",
        "## Failure-class deltas",
        "",
        "| Failure class | Candidate minus baseline |",
        "|---|---:|",
    ])
    if comparison.failure_class_deltas:
        for failure_class, delta in comparison.failure_class_deltas.items():
            lines.append(f"| {failure_class} | {delta:+d} |")
    else:
        lines.append("| - | 0 |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

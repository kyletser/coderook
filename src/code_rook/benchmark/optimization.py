from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.benchmark.compare import (
    BenchmarkComparison,
    compare_benchmark_reports,
)
from code_rook.benchmark.contract import find_candidate_contract_issues
from code_rook.benchmark.models import BenchmarkReport

OptimizationCategory = Literal[
    "retrieval",
    "editing",
    "verification",
    "permission",
    "budget",
    "model_error",
]
ExperimentOutcome = Literal["accepted", "rejected", "inconclusive"]

_FAILURE_CATEGORIES: dict[str, OptimizationCategory] = {
    "retrieval_failure": "retrieval",
    "understanding_error": "retrieval",
    "incorrect_edit": "editing",
    "forbidden_change": "editing",
    "unexpected_change": "editing",
    "verification_failure": "verification",
    "permission_blocked": "permission",
    "budget_exhausted": "budget",
    "token_budget_exceeded": "budget",
    "cost_budget_exceeded": "budget",
    "model_error": "model_error",
    "runtime_error": "model_error",
    "unclassified": "model_error",
}
_INVESTIGATIONS: dict[OptimizationCategory, str] = {
    "retrieval": "审计 repo map、working set、符号引用和上下文选择理由",
    "editing": "审计编辑格式、PatchPlan、非目标变更与首改正确率",
    "verification": "审计诊断、测试选择、verifier 输出和有限修复循环",
    "permission": "审计权限请求、审批等待、sandbox 降级与允许工具合同",
    "budget": "审计步数、wall/token/cost 预算、压缩和重复调用",
    "model_error": "审计 provider 错误、wire format、重试和结构化输出",
}


class OptimizationCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: OptimizationCategory
    occurrences: int = Field(ge=1)
    affected_reports: int = Field(ge=1)
    task_ids: list[str]
    failure_classes: dict[str, int]
    investigation: str


class OptimizationBacklog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: str
    repository_commit: str
    task_catalog_fingerprint: str
    fixture_fingerprint: str
    budget_fingerprint: str
    source_report_sha256: list[str]
    clusters: list[OptimizationCluster]
    ready_for_optimization: bool


class OptimizationExperiment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: str
    category: OptimizationCategory
    hypothesis: str = Field(min_length=8)
    task_ids: list[str] = Field(min_length=1)
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    baseline_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: ExperimentOutcome
    comparison: BenchmarkComparison


# 计算标准化报告内容的稳定 SHA-256
def report_sha256(report: BenchmarkReport) -> str:
    payload = report.model_dump_json().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# 将 runner 的细粒度失败原因归入六类效果优化域
def optimization_category(failure_class: str | None) -> OptimizationCategory:
    return _FAILURE_CATEGORIES.get(
        failure_class or "unclassified",
        "model_error",
    )


# 拒绝缺少候选身份或合同漂移的优化输入报告
def _validate_reports(reports: list[BenchmarkReport]) -> None:
    if not reports:
        raise ValueError("optimization backlog requires at least one report")
    for index, report in enumerate(reports, 1):
        issues = find_candidate_contract_issues(report)
        if issues:
            raise ValueError(
                f"optimization report {index} failed contract:\n- "
                + "\n- ".join(issues)
            )
    identities = {
        (
            report.repository_commit,
            report.run_config.task_catalog_fingerprint,
            report.run_config.fixture_fingerprint,
            report.run_config.budget_fingerprint,
        )
        for report in reports
    }
    if len(identities) != 1:
        raise ValueError("optimization reports changed commit/task/fixture/budget")


# 从重复原始报告生成按失败域排序的效果优化调查队列
def build_optimization_backlog(
    reports: list[BenchmarkReport],
) -> OptimizationBacklog:
    _validate_reports(reports)
    first = reports[0]
    failures: dict[OptimizationCategory, Counter[str]] = defaultdict(Counter)
    tasks: dict[OptimizationCategory, set[str]] = defaultdict(set)
    affected: dict[OptimizationCategory, set[int]] = defaultdict(set)
    for report_index, report in enumerate(reports):
        for result in report.results:
            if result.passed:
                continue
            failure_class = result.failure_class or "unclassified"
            category = optimization_category(failure_class)
            failures[category][failure_class] += 1
            tasks[category].add(result.task_id)
            affected[category].add(report_index)
    clusters = [
        OptimizationCluster(
            category=category,
            occurrences=sum(classes.values()),
            affected_reports=len(affected[category]),
            task_ids=sorted(tasks[category]),
            failure_classes=dict(sorted(classes.items())),
            investigation=_INVESTIGATIONS[category],
        )
        for category, classes in failures.items()
    ]
    clusters.sort(key=lambda cluster: (-cluster.occurrences, cluster.category))
    return OptimizationBacklog(
        generated_at=datetime.now(UTC).isoformat(),
        repository_commit=first.repository_commit,
        task_catalog_fingerprint=first.run_config.task_catalog_fingerprint,
        fixture_fingerprint=first.run_config.fixture_fingerprint,
        budget_fingerprint=first.run_config.budget_fingerprint,
        source_report_sha256=sorted(report_sha256(report) for report in reports),
        clusters=clusters,
        ready_for_optimization=bool(clusters),
    )


# 记录一个绑定前后报告与 commit 的优化实验并客观判定结果
def record_optimization_experiment(
    *,
    category: OptimizationCategory,
    hypothesis: str,
    task_ids: list[str],
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
) -> OptimizationExperiment:
    for label, report in (("baseline", baseline), ("candidate", candidate)):
        issues = find_candidate_contract_issues(report)
        if issues:
            raise ValueError(
                f"optimization {label} failed contract:\n- "
                + "\n- ".join(issues)
            )
    if baseline.repository_commit == candidate.repository_commit:
        raise ValueError("optimization experiment requires different commits")
    baseline_identity = (
        baseline.suite,
        baseline.run_config.route_id,
        baseline.run_config.model,
        baseline.run_config.wire_format,
        baseline.run_config.config_fingerprint,
    )
    candidate_identity = (
        candidate.suite,
        candidate.run_config.route_id,
        candidate.run_config.model,
        candidate.run_config.wire_format,
        candidate.run_config.config_fingerprint,
    )
    if baseline_identity != candidate_identity:
        raise ValueError("optimization experiment changed suite/route/model/wire/config")
    comparison = compare_benchmark_reports(baseline, candidate)
    available = {result.task_id for result in baseline.results} & {
        result.task_id for result in candidate.results
    }
    selected = sorted(set(task_ids))
    missing = sorted(set(selected) - available)
    if missing:
        raise ValueError(f"optimization task ids are missing: {missing}")
    if not selected:
        raise ValueError("optimization experiment requires task ids")
    baseline_by_id = {result.task_id: result for result in baseline.results}
    wrong_domain = [
        task_id
        for task_id in selected
        if baseline_by_id[task_id].passed
        or optimization_category(baseline_by_id[task_id].failure_class) != category
    ]
    if wrong_domain:
        raise ValueError(
            f"optimization tasks do not fail in {category}: {wrong_domain}"
        )
    improvements = {
        transition.task_id for transition in comparison.improvements
    }
    regressions = {transition.task_id for transition in comparison.regressions}
    if comparison.gate_passed and improvements.intersection(selected):
        outcome: ExperimentOutcome = "accepted"
    elif not comparison.gate_passed or regressions.intersection(selected):
        outcome = "rejected"
    else:
        outcome = "inconclusive"
    return OptimizationExperiment(
        generated_at=datetime.now(UTC).isoformat(),
        category=category,
        hypothesis=hypothesis,
        task_ids=selected,
        baseline_commit=baseline.repository_commit,
        candidate_commit=candidate.repository_commit,
        baseline_report_sha256=report_sha256(baseline),
        candidate_report_sha256=report_sha256(candidate),
        outcome=outcome,
        comparison=comparison,
    )


# 写出优化队列的 JSON 与人类可审阅 Markdown
def write_optimization_backlog(
    backlog: OptimizationBacklog,
    output: Path,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "optimization-backlog.json").write_text(
        backlog.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    lines = [
        "# Benchmark Optimization Backlog",
        "",
        f"- Commit: `{backlog.repository_commit}`",
        f"- Ready: **{'yes' if backlog.ready_for_optimization else 'no'}**",
        "",
        "| Domain | Occurrences | Reports | Tasks | Investigation |",
        "|---|---:|---:|---|---|",
    ]
    for cluster in backlog.clusters:
        lines.append(
            f"| {cluster.category} | {cluster.occurrences} | "
            f"{cluster.affected_reports} | {', '.join(cluster.task_ids)} | "
            f"{cluster.investigation} |"
        )
    (output / "optimization-backlog.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# 写出绑定前后报告的单项优化实验记录
def write_optimization_experiment(
    experiment: OptimizationExperiment,
    output: Path,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "optimization-experiment.json").write_text(
        experiment.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

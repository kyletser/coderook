from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from statistics import fmean
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.benchmark.contract import find_candidate_contract_issues
from code_rook.benchmark.models import BenchmarkReport


class AggregatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_groups: int = Field(default=2, ge=1)
    expected_repeats: int = Field(default=2, ge=1)
    expected_wire_formats: int = Field(default=2, ge=1)
    expected_suite: str = "release"
    min_overall_pass_rate: float = Field(default=0.80, ge=0, le=1)
    min_multi_file_pass_rate: float = Field(default=0.75, ge=0, le=1)
    min_read_only_pass_rate: float = Field(default=0.90, ge=0, le=1)
    min_security_pass_rate: float = Field(default=1.0, ge=0, le=1)
    max_pass_rate_spread: float = Field(default=0.10, ge=0, le=1)


class AggregateMetric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mean: float
    minimum: float
    maximum: float


class AggregateTaskStability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    passed_repeats: int = Field(ge=0)
    repeats: int = Field(ge=1)
    pass_rate: float = Field(ge=0, le=1)


class AggregateGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_id: str
    model: str
    wire_format: str
    config_fingerprint: str
    candidate_fingerprint: str
    repeats: int = Field(ge=1)
    pass_rate: AggregateMetric
    categories: dict[str, AggregateMetric]
    cost_p95_mean_usd: float | None = None
    duration_p95_mean_s: float | None = None
    unstable_tasks: list[AggregateTaskStability] = Field(default_factory=list)


class BenchmarkAggregate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: str
    repository_commit: str
    task_catalog_fingerprint: str
    fixture_fingerprint: str
    budget_fingerprint: str
    report_count: int = Field(ge=0)
    groups: list[AggregateGroup]
    gate_passed: bool
    gate_reasons: list[str]
    policy: AggregatePolicy


# 汇总一组浮点样本的均值、最小值与最大值
def _metric(values: list[float]) -> AggregateMetric:
    return AggregateMetric(
        mean=fmean(values),
        minimum=min(values),
        maximum=max(values),
    )


# 仅在所有重复都存在可信数值时计算均值，避免把 unknown 当零
def _complete_mean(values: list[float | None]) -> float | None:
    if not values or any(value is None for value in values):
        return None
    return fmean(value for value in values if value is not None)


# 对同 route/model/wire 的重复报告汇总波动、类别和不稳定任务
def _aggregate_group(reports: list[BenchmarkReport]) -> AggregateGroup:
    first = reports[0]
    category_names = sorted(
        {category for report in reports for category in report.summary.categories}
    )
    categories = {
        category: _metric(
            [
                report.summary.categories[category].pass_rate
                for report in reports
                if category in report.summary.categories
            ]
        )
        for category in category_names
    }
    task_outcomes: dict[str, list[bool]] = defaultdict(list)
    for report in reports:
        for result in report.results:
            task_outcomes[result.task_id].append(result.passed)
    unstable = []
    for task_id, outcomes in sorted(task_outcomes.items()):
        passed = sum(outcomes)
        if 0 < passed < len(outcomes):
            unstable.append(
                AggregateTaskStability(
                    task_id=task_id,
                    passed_repeats=passed,
                    repeats=len(outcomes),
                    pass_rate=passed / len(outcomes),
                )
            )
    return AggregateGroup(
        route_id=first.run_config.route_id,
        model=first.run_config.model,
        wire_format=first.run_config.wire_format,
        config_fingerprint=first.run_config.config_fingerprint,
        candidate_fingerprint=first.run_config.candidate_fingerprint,
        repeats=len(reports),
        pass_rate=_metric([report.pass_rate for report in reports]),
        categories=categories,
        cost_p95_mean_usd=_complete_mean(
            [report.summary.cost_p95_usd for report in reports]
        ),
        duration_p95_mean_s=_complete_mean(
            [report.summary.elapsed_p95_s for report in reports]
        ),
        unstable_tasks=unstable,
    )


# 校验并聚合两个 wire format 的重复候选报告，输出唯一发布门禁结论
def aggregate_benchmark_reports(
    reports: list[BenchmarkReport],
    policy: AggregatePolicy | None = None,
) -> BenchmarkAggregate:
    selected = policy or AggregatePolicy()
    if not reports:
        raise ValueError("benchmark aggregate requires at least one report")
    for index, report in enumerate(reports, 1):
        issues = find_candidate_contract_issues(report)
        if issues:
            raise ValueError(
                f"candidate report {index} failed contract:\n- "
                + "\n- ".join(issues)
            )

    reasons: list[str] = []
    expected_reports = selected.expected_groups * selected.expected_repeats
    if len(reports) != expected_reports:
        reasons.append("report_count_mismatch")
    if {report.suite for report in reports} != {selected.expected_suite}:
        reasons.append("suite_mismatch")
    commits = {report.repository_commit for report in reports}
    task_fingerprints = {
        report.run_config.task_catalog_fingerprint for report in reports
    }
    fixture_fingerprints = {
        report.run_config.fixture_fingerprint for report in reports
    }
    budget_fingerprints = {
        report.run_config.budget_fingerprint for report in reports
    }
    if len(commits) != 1:
        reasons.append("repository_commit_changed")
    if len(task_fingerprints) != 1:
        reasons.append("task_contract_changed")
    if len(fixture_fingerprints) != 1:
        reasons.append("fixture_changed")
    if len(budget_fingerprints) != 1:
        reasons.append("budget_changed")

    grouped: dict[tuple[str, str, str, str], list[BenchmarkReport]] = defaultdict(list)
    for report in reports:
        config = report.run_config
        identity = (
            config.route_id,
            config.model,
            config.wire_format,
            config.config_fingerprint,
        )
        grouped[identity].append(report)
    if len(grouped) != selected.expected_groups:
        reasons.append("group_count_mismatch")
    wire_formats = {key[2] for key in grouped}
    if len(wire_formats) != selected.expected_wire_formats:
        reasons.append("wire_format_count_mismatch")

    groups = [
        _aggregate_group(group_reports)
        for _identity, group_reports in sorted(grouped.items())
    ]
    for group in groups:
        label = f"{group.route_id}:{group.wire_format}"
        source_group = grouped[
            (
                group.route_id,
                group.model,
                group.wire_format,
                group.config_fingerprint,
            )
        ]
        if len(
            {report.run_config.candidate_fingerprint for report in source_group}
        ) != 1:
            reasons.append(f"candidate_identity_changed:{label}")
        if group.repeats != selected.expected_repeats:
            reasons.append(f"repeat_count_mismatch:{label}")
        if group.pass_rate.mean < selected.min_overall_pass_rate:
            reasons.append(f"overall_pass_rate_below_threshold:{label}")
        if group.pass_rate.maximum - group.pass_rate.minimum > selected.max_pass_rate_spread:
            reasons.append(f"pass_rate_spread_exceeded:{label}")
        category_thresholds = {
            "multi_file_change": selected.min_multi_file_pass_rate,
            "explain": selected.min_read_only_pass_rate,
            "security_negative": selected.min_security_pass_rate,
        }
        for category, threshold in category_thresholds.items():
            metric = group.categories.get(category)
            if metric is None or metric.mean < threshold:
                reasons.append(f"{category}_pass_rate_below_threshold:{label}")

    return BenchmarkAggregate(
        generated_at=datetime.now(UTC).isoformat(),
        repository_commit=(next(iter(commits)) if len(commits) == 1 else "mixed"),
        task_catalog_fingerprint=(
            next(iter(task_fingerprints)) if len(task_fingerprints) == 1 else "mixed"
        ),
        fixture_fingerprint=(
            next(iter(fixture_fingerprints))
            if len(fixture_fingerprints) == 1
            else "mixed"
        ),
        budget_fingerprint=(
            next(iter(budget_fingerprints))
            if len(budget_fingerprints) == 1
            else "mixed"
        ),
        report_count=len(reports),
        groups=groups,
        gate_passed=not reasons,
        gate_reasons=reasons,
        policy=selected,
    )

#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from code_rook.benchmark.aggregate import (
    AggregatePolicy,
    BenchmarkAggregate,
    aggregate_benchmark_reports,
)
from code_rook.benchmark.compare import load_benchmark_report


# 定义四份 release 候选报告的聚合阈值与输出参数
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate repeated CodeRook benchmark candidate reports."
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/aggregate"),
    )
    parser.add_argument("--expected-groups", type=int, default=2)
    parser.add_argument("--expected-repeats", type=int, default=2)
    parser.add_argument("--expected-wire-formats", type=int, default=2)
    parser.add_argument("--min-overall-pass-rate", type=float, default=0.80)
    parser.add_argument("--min-multi-file-pass-rate", type=float, default=0.75)
    parser.add_argument("--min-read-only-pass-rate", type=float, default=0.90)
    parser.add_argument("--min-security-pass-rate", type=float, default=1.0)
    parser.add_argument("--max-pass-rate-spread", type=float, default=0.10)
    parser.add_argument("--report-only", action="store_true")
    return parser


# 将聚合结论、每组波动与不稳定任务写为 Markdown
def _render_markdown(aggregate: BenchmarkAggregate) -> str:
    verdict = "PASS" if aggregate.gate_passed else "FAIL"
    lines = [
        "# CodeRook Release Benchmark Aggregate",
        "",
        f"- Gate: **{verdict}**",
        f"- Generated: `{aggregate.generated_at}`",
        f"- Repository commit: `{aggregate.repository_commit}`",
        f"- Reports / groups: **{aggregate.report_count} / {len(aggregate.groups)}**",
        "- Task / fixture / budget fingerprints: "
        f"`{aggregate.task_catalog_fingerprint}` / "
        f"`{aggregate.fixture_fingerprint}` / `{aggregate.budget_fingerprint}`",
        "- Gate reasons: **"
        + (", ".join(aggregate.gate_reasons) if aggregate.gate_reasons else "none")
        + "**",
        "",
        "| Route | Model | Wire | Repeats | Pass mean | Min | Max | Cost P95 mean | Duration P95 mean |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for group in aggregate.groups:
        cost = (
            "unknown"
            if group.cost_p95_mean_usd is None
            else f"${group.cost_p95_mean_usd:.4f}"
        )
        duration = (
            "unknown"
            if group.duration_p95_mean_s is None
            else f"{group.duration_p95_mean_s:.1f}s"
        )
        lines.append(
            f"| {group.route_id} | {group.model} | {group.wire_format} | "
            f"{group.repeats} | {group.pass_rate.mean:.1%} | "
            f"{group.pass_rate.minimum:.1%} | {group.pass_rate.maximum:.1%} | "
            f"{cost} | {duration} |"
        )
    lines.extend(
        [
            "",
            "## Category means",
            "",
            "| Route / wire | Category | Mean | Min | Max |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for group in aggregate.groups:
        for category, metric in sorted(group.categories.items()):
            lines.append(
                f"| {group.route_id} / {group.wire_format} | {category} | "
                f"{metric.mean:.1%} | {metric.minimum:.1%} | {metric.maximum:.1%} |"
            )
    lines.extend(
        [
            "",
            "## Unstable tasks",
            "",
            "| Route / wire | Task | Passed repeats | Pass rate |",
            "|---|---|---:|---:|",
        ]
    )
    unstable_rows = 0
    for group in aggregate.groups:
        for task in group.unstable_tasks:
            unstable_rows += 1
            lines.append(
                f"| {group.route_id} / {group.wire_format} | `{task.task_id}` | "
                f"{task.passed_repeats}/{task.repeats} | {task.pass_rate:.1%} |"
            )
    if unstable_rows == 0:
        lines.append("| - | - | 0 | - |")
    lines.extend(
        [
            "",
            "A passing aggregate proves only the pinned reports and policy above. "
            "It is not an official SWE-bench or Aider leaderboard result.",
            "",
        ]
    )
    return "\n".join(lines)


# 加载 input root 下全部 report.json，聚合并写出 JSON/Markdown
def main() -> int:
    args = _build_parser().parse_args()
    paths = sorted(args.input_root.rglob("report.json"))
    if not paths:
        raise SystemExit(f"no report.json files under {args.input_root}")
    policy = AggregatePolicy(
        expected_groups=args.expected_groups,
        expected_repeats=args.expected_repeats,
        expected_wire_formats=args.expected_wire_formats,
        min_overall_pass_rate=args.min_overall_pass_rate,
        min_multi_file_pass_rate=args.min_multi_file_pass_rate,
        min_read_only_pass_rate=args.min_read_only_pass_rate,
        min_security_pass_rate=args.min_security_pass_rate,
        max_pass_rate_spread=args.max_pass_rate_spread,
    )
    try:
        aggregate = aggregate_benchmark_reports(
            [load_benchmark_report(path) for path in paths],
            policy,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "aggregate.json").write_text(
        aggregate.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (args.output / "aggregate.md").write_text(
        _render_markdown(aggregate),
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Benchmark aggregate: {'PASS' if aggregate.gate_passed else 'FAIL'}; "
        f"reports={aggregate.report_count}; groups={len(aggregate.groups)}"
    )
    print(f"Reports: {args.output.resolve()}")
    return 0 if args.report_only or aggregate.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

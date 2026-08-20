#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from code_rook.benchmark.compare import (
    ComparisonPolicy,
    compare_benchmark_reports,
    load_benchmark_report,
    write_comparison_json,
    write_comparison_markdown,
)


# 定义基线/候选 benchmark 回归比较器的命令行参数
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare two CodeRook benchmark reports and enforce regression policy."
    )
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".benchmark-results/comparison"))
    parser.add_argument("--max-pass-rate-drop", type=float, default=0.0)
    parser.add_argument("--max-verifier-rate-drop", type=float, default=0.0)
    parser.add_argument("--max-cost-p95-increase-rate", type=float, default=0.25)
    parser.add_argument("--max-duration-p95-increase-rate", type=float, default=0.25)
    parser.add_argument("--allow-task-set-change", action="store_true")
    parser.add_argument("--allow-contract-change", action="store_true")
    parser.add_argument("--allow-task-regression", action="store_true")
    parser.add_argument("--allow-security-negative-failure", action="store_true")
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Always exit zero after writing the comparison reports.",
    )
    return parser


# 加载两份报告、执行回归策略并写出 JSON 与 Markdown 证据
def main() -> int:
    args = _build_parser().parse_args()
    policy = ComparisonPolicy(
        max_pass_rate_drop=args.max_pass_rate_drop,
        max_verifier_rate_drop=args.max_verifier_rate_drop,
        max_cost_p95_increase_rate=args.max_cost_p95_increase_rate,
        max_duration_p95_increase_rate=args.max_duration_p95_increase_rate,
        require_same_tasks=not args.allow_task_set_change,
        require_same_contract=not args.allow_contract_change,
        fail_on_task_regression=not args.allow_task_regression,
        require_security_negative_pass=not args.allow_security_negative_failure,
    )
    comparison = compare_benchmark_reports(
        load_benchmark_report(args.baseline),
        load_benchmark_report(args.candidate),
        policy,
    )
    write_comparison_json(comparison, args.output / "comparison.json")
    write_comparison_markdown(comparison, args.output / "comparison.md")
    print(
        f"Benchmark comparison: {'PASS' if comparison.gate_passed else 'FAIL'}; "
        f"regressions={len(comparison.regressions)}, "
        f"improvements={len(comparison.improvements)}"
    )
    print(f"Reports: {args.output.resolve()}")
    return 0 if args.report_only or comparison.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast

from code_rook.benchmark.compare import load_benchmark_report
from code_rook.benchmark.optimization import (
    OptimizationCategory,
    build_optimization_backlog,
    record_optimization_experiment,
    write_optimization_backlog,
    write_optimization_experiment,
)

_CATEGORIES = (
    "retrieval",
    "editing",
    "verification",
    "permission",
    "budget",
    "model_error",
)


# 定义优化队列生成与前后实验记录的命令行合同
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and record evidence-bound benchmark optimizations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("reports", type=Path, nargs="*")
    plan.add_argument("--input-root", type=Path)
    plan.add_argument("--output", type=Path, required=True)
    record = subparsers.add_parser("record")
    record.add_argument("baseline", type=Path)
    record.add_argument("candidate", type=Path)
    record.add_argument("--category", choices=_CATEGORIES, required=True)
    record.add_argument("--hypothesis", required=True)
    record.add_argument("--task", action="append", required=True)
    record.add_argument("--output", type=Path, required=True)
    return parser


# 生成失败聚类队列或记录绑定前后 commit 的实验结论
def main() -> int:
    args = _parser().parse_args()
    if args.command == "plan":
        paths = list(args.reports)
        if args.input_root is not None:
            paths.extend(sorted(args.input_root.rglob("report.json")))
        if not paths:
            raise SystemExit("optimization plan requires benchmark reports")
        backlog = build_optimization_backlog(
            [load_benchmark_report(path) for path in paths]
        )
        write_optimization_backlog(backlog, args.output)
        print(
            f"Optimization backlog: clusters={len(backlog.clusters)} "
            f"output={args.output.resolve()}"
        )
        return 0
    experiment = record_optimization_experiment(
        category=cast(OptimizationCategory, args.category),
        hypothesis=args.hypothesis,
        task_ids=args.task,
        baseline=load_benchmark_report(args.baseline),
        candidate=load_benchmark_report(args.candidate),
    )
    write_optimization_experiment(experiment, args.output)
    print(
        f"Optimization experiment: {experiment.outcome}; "
        f"output={args.output.resolve()}"
    )
    return 0 if experiment.outcome == "accepted" else 1


if __name__ == "__main__":
    raise SystemExit(main())

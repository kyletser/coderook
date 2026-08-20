#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from code_rook.benchmark.swebench import (
    build_swebench_harness_command,
    build_swebench_prediction,
    load_swebench_instances,
    write_swebench_predictions,
)


# 定义从已修改工作区导出官方 SWE-bench prediction 的参数
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export CodeRook-modified SWE-bench workspaces to the official prediction format."
        )
    )
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument(
        "--workspaces",
        type=Path,
        required=True,
        help="Directory containing one Git workspace per instance_id.",
    )
    parser.add_argument("--model-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--dataset-name",
        default="princeton-nlp/SWE-bench_Lite",
    )
    parser.add_argument("--split", default="test")
    parser.add_argument("--run-id", default="coderook-smoke")
    parser.add_argument("--max-workers", type=int, default=1)
    parser.add_argument(
        "--print-harness-command",
        action="store_true",
        help="Print the official evaluation command after exporting predictions.",
    )
    return parser


# 校验各实例基线并输出包含新增文件的标准 patch/prediction
def main() -> int:
    args = _build_parser().parse_args()
    instances = load_swebench_instances(args.instances)
    predictions = [
        build_swebench_prediction(
            instance,
            args.workspaces / instance.instance_id,
            args.model_name,
        )
        for instance in instances
    ]
    write_swebench_predictions(predictions, args.output)
    print(f"Exported {len(predictions)} SWE-bench prediction(s): {args.output.resolve()}")
    if args.print_harness_command:
        command = build_swebench_harness_command(
            args.output.resolve(),
            dataset_name=args.dataset_name,
            split=args.split,
            run_id=args.run_id,
            max_workers=args.max_workers,
            instance_ids=[instance.instance_id for instance in instances],
        )
        print(shlex.join(command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

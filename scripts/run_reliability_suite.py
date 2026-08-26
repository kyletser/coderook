from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_STAGE_BUDGETS = {
    "task_router": 2.0,
    "compaction": 5.0,
    "multiagent": 10.0,
    "internal_repeat_1": 6.0,
    "internal_repeat_2": 6.0,
    "polyglot": 6.0,
}


# 返回当前候选完整 Git commit
def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 为单个真实模型阶段创建独立硬预算账本环境且不修改用户凭据
def _stage_environment(output: Path, stage: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEROOK_EXPERIMENT_BUDGET_FILE"] = str((output / "budgets" / f"{stage}.json").resolve())
    env["CODEROOK_EXPERIMENT_BUDGET_USD"] = str(_STAGE_BUDGETS[stage])
    return env


# 执行一个完整实验块并记录命令、退出码和预算证据路径
def _run_stage(
    output: Path,
    stage: str,
    command: list[str],
    *,
    real_model: bool,
) -> dict[str, Any]:
    env = _stage_environment(output, stage) if real_model else dict(os.environ)
    if not real_model:
        env.pop("CODEROOK_EXPERIMENT_BUDGET_FILE", None)
        env.pop("CODEROOK_EXPERIMENT_BUDGET_USD", None)
    started = datetime.now(UTC).isoformat()
    result = subprocess.run(command, check=False, env=env)
    return {
        "stage": stage,
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "command": command,
        "real_model": real_model,
        "budget_usd": _STAGE_BUDGETS.get(stage, 0.0),
        "budget_ledger": (str(output / "budgets" / f"{stage}.json") if real_model else None),
        "exit_code": result.returncode,
        "completed": result.returncode in {0, 1},
    }


# 构造固定阶段命令，公开切片只在显式提供数据集 commit 时加入
def _commands(args: argparse.Namespace) -> list[tuple[str, list[str], bool]]:
    python = sys.executable
    root = args.output.resolve()
    stages: list[tuple[str, list[str], bool]] = [
        (
            "task_router",
            [
                python,
                "scripts/run_strategy_router_experiment.py",
                "--max-cost-usd",
                str(_STAGE_BUDGETS["task_router"]),
                "--output",
                str(root / "task-router"),
            ],
            True,
        ),
        (
            "compaction",
            [
                python,
                "scripts/run_compaction_experiment.py",
                "--max-cost-usd",
                str(_STAGE_BUDGETS["compaction"]),
                "--output",
                str(root / "compaction"),
            ],
            True,
        ),
        (
            "multiagent",
            [
                python,
                "scripts/run_multiagent_strategy_experiment.py",
                "--max-cost-usd",
                str(_STAGE_BUDGETS["multiagent"]),
                "--output",
                str(root / "multiagent"),
            ],
            True,
        ),
        (
            "crash_recovery",
            [
                python,
                "scripts/run_crash_recovery_matrix.py",
                "--iterations",
                "100",
                "--output",
                str(root / "crash-recovery" / "report.json"),
            ],
            False,
        ),
    ]
    for repeat in (1, 2):
        stage = f"internal_repeat_{repeat}"
        stages.append(
            (
                stage,
                [
                    python,
                    "scripts/run_benchmark.py",
                    "--suite",
                    "release",
                    "--report-only",
                    "--temperature",
                    "0",
                    "--output",
                    str(root / "internal" / f"repeat-{repeat}"),
                ],
                True,
            )
        )
    if args.polyglot_dataset and args.polyglot_commit:
        stages.append(
            (
                "polyglot",
                [
                    python,
                    "scripts/run_polyglot_benchmark.py",
                    "--dataset",
                    str(args.polyglot_dataset.resolve()),
                    "--expected-commit",
                    args.polyglot_commit,
                    "--fixed-slice-per-language",
                    "3",
                    "--slice-seed",
                    "coderook-v1",
                    "--temperature",
                    "0",
                    "--max-cost",
                    "0.33",
                    "--output",
                    str(root / "polyglot"),
                ],
                True,
            )
        )
    return stages


# 生成可审计执行计划或按顺序运行，任一基础设施失败后停止后续真实模型组
def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CodeRook reliability evidence suite")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/reliability"),
    )
    parser.add_argument("--polyglot-dataset", type=Path)
    parser.add_argument("--polyglot-commit")
    args = parser.parse_args()
    if bool(args.polyglot_dataset) != bool(args.polyglot_commit):
        raise SystemExit("polyglot dataset and commit must be supplied together")
    if sum(_STAGE_BUDGETS.values()) != 35.0:
        raise SystemExit("stage budgets must sum to exactly 35 USD")
    commands = _commands(args)
    plan = {
        "schema_version": 1,
        "commit": _commit(),
        "temperature": 0,
        "route": "legacy-anthropic / deepseek-v4-flash",
        "total_real_model_budget_usd": 35.0,
        "stages": [
            {
                "stage": stage,
                "command": command,
                "real_model": real_model,
                "budget_usd": _STAGE_BUDGETS.get(stage, 0.0),
            }
            for stage, command, real_model in commands
        ],
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "suite-plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if not args.execute:
        print(f"Reliability suite plan: {args.output / 'suite-plan.json'}")
        print("No model calls were made; pass --execute to run the frozen suite.")
        return 0
    results: list[dict[str, Any]] = []
    for stage, command, real_model in commands:
        result = _run_stage(
            args.output,
            stage,
            command,
            real_model=real_model,
        )
        results.append(result)
        if not result["completed"]:
            break
    suite_report = {**plan, "executed_at": datetime.now(UTC).isoformat(), "results": results}
    (args.output / "suite-report.json").write_text(
        json.dumps(suite_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0 if len(results) == len(commands) else 2


if __name__ == "__main__":
    raise SystemExit(main())

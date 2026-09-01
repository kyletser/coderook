from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_rook.benchmark.experiment import (
    candidate_git_state,
    resolve_experiment_candidate,
)
from code_rook.core.config import get_config

_PILOT_BUDGETS = {
    "compaction": 2.0,
    "multiagent": 4.0,
    "internal_quick": 2.0,
}

_FULL_BUDGETS = {
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
def _stage_environment(
    output: Path,
    stage: str,
    budget_usd: float,
) -> dict[str, str]:
    env = dict(os.environ)
    env["CODEROOK_EXPERIMENT_BUDGET_FILE"] = str(
        (output / "budgets" / f"{stage}.json").resolve()
    )
    env["CODEROOK_EXPERIMENT_BUDGET_USD"] = str(budget_usd)
    return env


# 执行一个完整实验块并记录命令、退出码和预算证据路径
def _run_stage(
    output: Path,
    stage: str,
    command: list[str],
    *,
    real_model: bool,
    budget_usd: float,
) -> dict[str, Any]:
    env = (
        _stage_environment(output, stage, budget_usd)
        if real_model
        else dict(os.environ)
    )
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
        "budget_usd": budget_usd,
        "budget_ledger": (
            str(output / "budgets" / f"{stage}.json") if real_model else None
        ),
        "exit_code": result.returncode,
        "completed": result.returncode in {0, 1},
    }


# 构造默认低成本 Pilot，完整矩阵必须通过显式 profile 启用
def _commands(args: argparse.Namespace) -> list[tuple[str, list[str], bool, float]]:
    python = sys.executable
    root = args.output.resolve()
    stages: list[tuple[str, list[str], bool, float]] = []
    if args.profile == "pilot":
        stages.extend(
            [
                (
                    "compaction",
                    [
                        python,
                        "scripts/run_compaction_experiment.py",
                        "--task-limit",
                        "6",
                        "--repeats",
                        "1",
                        "--max-cost-usd",
                        str(_PILOT_BUDGETS["compaction"]),
                        "--output",
                        str(root / "compaction"),
                    ],
                    True,
                    _PILOT_BUDGETS["compaction"],
                ),
                (
                    "multiagent",
                    [
                        python,
                        "scripts/run_multiagent_strategy_experiment.py",
                        "--multi-limit",
                        "3",
                        "--quick-limit",
                        "3",
                        "--max-cost-usd",
                        str(_PILOT_BUDGETS["multiagent"]),
                        "--output",
                        str(root / "multiagent"),
                    ],
                    True,
                    _PILOT_BUDGETS["multiagent"],
                ),
                (
                    "internal_quick",
                    [
                        python,
                        "scripts/run_benchmark.py",
                        "--suite",
                        "quick",
                        "--report-only",
                        "--temperature",
                        "0",
                        "--output",
                        str(root / "internal" / "quick"),
                    ],
                    True,
                    _PILOT_BUDGETS["internal_quick"],
                ),
            ]
        )
        return stages

    stages.extend(
        [
            (
                "compaction",
                [
                    python,
                    "scripts/run_compaction_experiment.py",
                    "--task-limit",
                    "12",
                    "--repeats",
                    "2",
                    "--max-cost-usd",
                    str(_FULL_BUDGETS["compaction"]),
                    "--output",
                    str(root / "compaction"),
                ],
                True,
                _FULL_BUDGETS["compaction"],
            ),
            (
                "multiagent",
                [
                    python,
                    "scripts/run_multiagent_strategy_experiment.py",
                    "--multi-limit",
                    "10",
                    "--quick-limit",
                    "10",
                    "--max-cost-usd",
                    str(_FULL_BUDGETS["multiagent"]),
                    "--output",
                    str(root / "multiagent"),
                ],
                True,
                _FULL_BUDGETS["multiagent"],
            ),
        ]
    )
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
                _FULL_BUDGETS[stage],
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
                _FULL_BUDGETS["polyglot"],
            )
        )
    return stages


# 生成可审计执行计划或按顺序运行，任一基础设施失败后停止后续真实模型组
def main() -> int:
    parser = argparse.ArgumentParser(description="Run the CodeRook reliability evidence suite")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--profile", choices=("pilot", "full"), default="pilot")
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
    if args.profile != "full" and args.polyglot_dataset:
        raise SystemExit("polyglot is available only with --profile full")
    try:
        _resolved, candidate = resolve_experiment_candidate(
            get_config(),
            temperature=0.0,
        )
    except RuntimeError as exc:
        raise SystemExit(f"experiment preflight failed: {exc}") from exc
    git_state = candidate_git_state()
    commands = _commands(args)
    total_budget = sum(
        budget_usd
        for _stage, _command, real_model, budget_usd in commands
        if real_model
    )
    plan = {
        "schema_version": 2,
        "profile": args.profile,
        "commit": _commit(),
        "git": git_state,
        "temperature": 0,
        "candidate": candidate,
        "total_real_model_budget_usd": total_budget,
        "stages": [
            {
                "stage": stage,
                "command": command,
                "real_model": real_model,
                "budget_usd": budget_usd,
            }
            for stage, command, real_model, budget_usd in commands
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
    if not git_state["working_tree_clean"]:
        raise SystemExit("experiment execution requires a clean Git working tree")
    results: list[dict[str, Any]] = []
    for stage, command, real_model, budget_usd in commands:
        result = _run_stage(
            args.output,
            stage,
            command,
            real_model=real_model,
            budget_usd=budget_usd,
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

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from code_rook.benchmark.executor import CodeRookBenchmarkExecutor
from code_rook.benchmark.experiment import (
    candidate_git_state,
    configure_experiment_budget,
    resolve_experiment_candidate,
)
from code_rook.benchmark.loader import LoadedBenchmarkTask, load_benchmark_tasks
from code_rook.benchmark.models import BenchmarkRunConfig
from code_rook.benchmark.report import write_json_report, write_markdown_report
from code_rook.benchmark.runner import BenchmarkRunner
from code_rook.core.config import get_config
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.strategy import TaskStrategy


# 返回完整 Git commit用于绑定所有对照报告
def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 给非单 Agent 组补充受控 agent 工具，同时保留原任务工具边界
def _with_agent_tool(loaded: LoadedBenchmarkTask) -> LoadedBenchmarkTask:
    allowed = list(dict.fromkeys([*loaded.task.allowed_tools, "agent"]))
    return LoadedBenchmarkTask(
        task=loaded.task.model_copy(update={"allowed_tools": allowed}),
        manifest_path=loaded.manifest_path,
        fixture_path=loaded.fixture_path,
    )


# 构造绑定策略、模型、温度和任务切片的报告配置
def _run_config(
    policy: str,
    cohort: str,
    tasks: list[LoadedBenchmarkTask],
) -> BenchmarkRunConfig:
    config = get_config()
    route = RouteRegistry(config.llm, temperature_override=0.0).route()
    material = {
        "policy": policy,
        "cohort": cohort,
        "tasks": [loaded.task.id for loaded in tasks],
        "route": route.validation_digest(),
        "temperature": route.temperature,
    }
    fingerprint = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return BenchmarkRunConfig(
        route_id=route.id,
        model=route.model,
        wire_format=route.wire_format,
        router=config.llm.router,
        thinking=route.thinking,
        temperature=route.temperature,
        config_fingerprint=fingerprint,
        benchmark_name=f"multiagent-{policy}-{cohort}",
        dataset_name="coderook-50-fixed-cohorts",
        dataset_commit="coding-katas-v1",
    )


# 汇总单份 benchmark 报告的成本、Worker、冲突和主工作区污染指标
def _report_metrics(report: Any) -> dict[str, Any]:
    results = list(report.results)
    known_costs = [
        result.execution.estimated_cost_usd
        for result in results
        if result.execution.estimated_cost_usd is not None
    ]
    return {
        "total": report.total,
        "passed": report.passed,
        "pass_rate": report.pass_rate,
        "elapsed_s": sum(result.execution.elapsed_s for result in results),
        "tokens": sum(
            result.execution.input_tokens + result.execution.output_tokens for result in results
        ),
        "cost_usd": sum(known_costs) if len(known_costs) == len(results) else None,
        "workers": sum(result.execution.worker_count for result in results),
        "worker_conflicts": sum(result.execution.worker_conflicts for result in results),
        "worker_applies": sum(result.execution.worker_apply_count for result in results),
        "unreviewed_workspace_writes": sum(
            result.execution.unreviewed_workspace_writes for result in results
        ),
    }


# 运行单 Agent、盲目委派和路由委派三组真实任务对照并保留逐任务证据
async def _run(
    args: argparse.Namespace,
    *,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    catalog = load_benchmark_tasks(args.tasks, root)
    all_multi = [loaded for loaded in catalog if loaded.task.category == "multi_file_change"]
    all_quick = [loaded for loaded in catalog if "quick" in loaded.task.suites]
    if len(all_multi) != 10 or len(all_quick) != 10:
        raise SystemExit(
            f"expected 10 multi-file and 10 quick tasks, found "
            f"{len(all_multi)} and {len(all_quick)}"
        )
    multi = all_multi[: args.multi_limit]
    quick = all_quick[: args.quick_limit]
    config = get_config()
    policies = tuple(args.policy or ("single", "always_delegate", "routed"))
    cohorts = {"multi_file": multi, "quick": quick}
    rows: list[dict[str, Any]] = []
    spent = 0.0
    previous_block_cost = 0.0
    completed_blocks: list[str] = []
    for policy in policies:
        for cohort, base_tasks in cohorts.items():
            if spent >= args.max_cost_usd or (
                previous_block_cost > 0 and spent + previous_block_cost > args.max_cost_usd
            ):
                break
            config = get_config()
            config.agent.task_router = "hybrid"
            config.agent.delegation_policy = policy
            selected = (
                base_tasks
                if policy == "single"
                else [_with_agent_tool(loaded) for loaded in base_tasks]
            )
            block_dir = args.output / policy / cohort
            runner = BenchmarkRunner(
                CodeRookBenchmarkExecutor(
                    config,
                    temperature=0.0,
                    auto_apply_reviewed_workers=policy != "single",
                    strategy_override=(
                        TaskStrategy.DIRECT if policy == "single" else None
                    ),
                ),
                evidence_root=block_dir / "evidence",
            )
            report = await runner.run(
                selected,
                repository_commit=_commit(),
                suite=f"multiagent-{policy}-{cohort}",
                run_config=_run_config(policy, cohort, selected),
            )
            write_json_report(report, block_dir / "report.json")
            write_markdown_report(report, block_dir / "report.md")
            metrics = _report_metrics(report)
            rows.append({"policy": policy, "cohort": cohort, **metrics})
            block_cost = metrics["cost_usd"]
            if isinstance(block_cost, (int, float)):
                previous_block_cost = float(block_cost)
                spent += float(block_cost)
            completed_blocks.append(f"{policy}:{cohort}")
    return {
        "schema_version": 1,
        "experiment": "routed-multi-agent",
        "commit": _commit(),
        "dataset_fingerprint": hashlib.sha256(
            "\n".join(
                loaded.manifest_path.read_text(encoding="utf-8") for loaded in [*multi, *quick]
            ).encode("utf-8")
        ).hexdigest(),
        "route": {
            **candidate,
        },
        "budget": {"limit_usd": args.max_cost_usd, "spent_usd": spent},
        "completed_blocks": completed_blocks,
        "rows": rows,
    }


# 将三种策略和两个任务群的质量、成本、冲突与污染指标渲染成表格
def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Routed Multi-Agent Experiment",
        "",
        "| Policy | Cohort | Pass@1 | Time | Tokens | Cost | Workers | Conflicts | Unreviewed writes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        cost = "unknown" if row["cost_usd"] is None else f"${row['cost_usd']:.4f}"
        lines.append(
            f"| {row['policy']} | {row['cohort']} | {row['pass_rate']:.1%} | "
            f"{row['elapsed_s']:.1f}s | {row['tokens']} | {cost} | {row['workers']} | "
            f"{row['worker_conflicts']} | {row['unreviewed_workspace_writes']} |"
        )
    lines.extend(
        [
            "",
            "The experiment harness may approve only completed, verified, digest-bound Worker "
            "handoffs. It never pushes changes.",
        ]
    )
    return "\n".join(lines) + "\n"


# 解析低成本 Pilot、预检和完整策略块参数
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run routed multi-agent ablations")
    parser.add_argument("--tasks", type=Path, default=Path("benchmarks/tasks"))
    parser.add_argument("--max-cost-usd", type=float, default=4.0)
    parser.add_argument("--multi-limit", type=int, default=3)
    parser.add_argument("--quick-limit", type=int, default=3)
    parser.add_argument("--expected-model")
    parser.add_argument(
        "--policy",
        action="append",
        choices=("single", "always_delegate", "routed"),
        default=[],
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/reliability/multiagent"),
    )
    return parser


# 解析预算与目录参数，先完成零调用预检再执行并写出聚合报告
def main() -> int:
    args = _parser().parse_args()
    if args.max_cost_usd <= 0 or args.max_cost_usd > 35:
        raise SystemExit("--max-cost-usd must be in (0, 35]")
    if not 1 <= args.multi_limit <= 10 or not 1 <= args.quick_limit <= 10:
        raise SystemExit("task limits must be between 1 and 10")
    try:
        _resolved, candidate = resolve_experiment_candidate(
            get_config(),
            temperature=0.0,
            expected_model=args.expected_model,
        )
    except RuntimeError as exc:
        raise SystemExit(f"experiment preflight failed: {exc}") from exc
    git_state = candidate_git_state()
    preflight = {
        "candidate": candidate,
        "git": git_state,
        "policies": args.policy or ["single", "always_delegate", "routed"],
        "multi_file_tasks": args.multi_limit,
        "quick_tasks": args.quick_limit,
        "maximum_task_runs": (
            (len(args.policy) if args.policy else 3)
            * (args.multi_limit + args.quick_limit)
        ),
        "max_cost_usd": args.max_cost_usd,
        "model_calls": False,
    }
    if args.preflight:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0 if git_state["working_tree_clean"] else 2
    if not git_state["working_tree_clean"]:
        raise SystemExit("experiment requires a clean Git working tree")
    args.output.mkdir(parents=True, exist_ok=True)
    configure_experiment_budget(args.output, limit_usd=args.max_cost_usd)
    report = asyncio.run(_run(args, candidate=candidate))
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (args.output / "report.md").write_text(
        _markdown(report),
        encoding="utf-8",
        newline="\n",
    )
    print(f"Multi-agent evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

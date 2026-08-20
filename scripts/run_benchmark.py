from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

from code_rook.benchmark.contract import require_candidate_contract
from code_rook.benchmark.executor import CodeRookBenchmarkExecutor
from code_rook.benchmark.loader import (
    LoadedBenchmarkTask,
    load_benchmark_tasks,
    validate_benchmark_catalog,
)
from code_rook.benchmark.models import BenchmarkRunConfig
from code_rook.benchmark.report import write_json_report, write_markdown_report
from code_rook.benchmark.runner import BenchmarkRunner, verify_benchmark_baseline
from code_rook.core.config import CodeRookConfig, get_config
from code_rook.core.llm.route_registry import RouteRegistry


# 构建 benchmark 命令行参数
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run reproducible CodeRook benchmark tasks.")
    parser.add_argument("--tasks", type=Path, default=Path("benchmarks/tasks"))
    parser.add_argument("--task", action="append", default=[], help="Run only this task id.")
    parser.add_argument(
        "--suite",
        choices=("quick", "nightly", "release"),
        help="Run tasks assigned to this release gate.",
    )
    parser.add_argument("--list", action="store_true", help="List validated tasks and exit.")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate manifests and fixtures without calling a model.",
    )
    parser.add_argument(
        "--validate-baseline",
        action="store_true",
        help="Run verifiers and require every unmodified fixture to fail.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/latest"),
        help="Output directory for report.json and report.md.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="Fixed provider sampling temperature for reproducible model runs.",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write a valid candidate report without gating on 100%% task success.",
    )
    return parser


# 根据重复的 --task 参数筛选任务并拒绝未知编号
def _select_tasks(
    tasks: list[LoadedBenchmarkTask],
    selected_ids: list[str],
    suite: str | None,
) -> list[LoadedBenchmarkTask]:
    if suite is not None:
        tasks = [loaded for loaded in tasks if suite in loaded.task.suites]
    if not selected_ids:
        return tasks
    by_id = {loaded.task.id: loaded for loaded in tasks}
    missing = sorted(set(selected_ids) - by_id.keys())
    if missing:
        raise SystemExit(f"Unknown benchmark task id(s): {', '.join(missing)}")
    return [by_id[task_id] for task_id in selected_ids]


# 读取当前 Git commit；非 Git 环境显式返回 unknown
def _repository_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 构造不含凭据的固定 route 配置指纹，写入每份真实模型报告
def _benchmark_run_config(
    config: CodeRookConfig,
    temperature: float,
) -> BenchmarkRunConfig:
    registry = RouteRegistry(config.llm, temperature_override=temperature)
    route = registry.route()
    material = {
        "route_id": route.id,
        "provider": route.provider,
        "wire_format": route.wire_format,
        "base_url": str(route.base_url),
        "model": route.model,
        "thinking": route.thinking,
        "temperature": route.temperature,
        "router": config.llm.router,
        "router_plan_route": config.llm.router_plan_route,
        "router_act_route": config.llm.router_act_route,
        "router_cost_budget": config.llm.router_cost_budget,
        "router_cost_fallback": config.llm.router_cost_fallback,
        "compaction_auto_threshold": config.compaction.auto_threshold,
        "compaction_retain_ratio": config.compaction.retain_ratio,
        "compaction_tool_result_limit": config.compaction.tool_result_limit,
        "compaction_tool_result_keep": config.compaction.tool_result_keep,
        "compaction_tool_result_summarize_threshold": (
            config.compaction.tool_result_summarize_threshold
        ),
        "permission_mode": "allow_list",
        "max_step_continues": 0,
        "dataset_commit": "coding-katas-v1",
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return BenchmarkRunConfig(
        route_id=route.id,
        model=route.model,
        wire_format=route.wire_format,
        router=config.llm.router,
        thinking=route.thinking,
        temperature=route.temperature,
        config_fingerprint=hashlib.sha256(encoded).hexdigest(),
        dataset_commit="coding-katas-v1",
    )


# 执行异步 benchmark 并写出两种报告格式
async def _run(args: argparse.Namespace, root: Path) -> int:
    if not 0.0 <= args.temperature <= 2.0:
        raise SystemExit("--temperature must be between 0 and 2")
    tasks = load_benchmark_tasks(args.tasks, root)
    validate_benchmark_catalog(tasks)
    selected = _select_tasks(tasks, args.task, args.suite)
    if not selected:
        raise SystemExit("No benchmark tasks matched the selected suite/task filters.")
    if args.validate_baseline:
        unexpected_passes: list[str] = []
        for loaded in selected:
            results = await verify_benchmark_baseline(loaded)
            if results and all(result.passed for result in results):
                unexpected_passes.append(loaded.task.id)
        if unexpected_passes:
            raise SystemExit(
                "Baseline unexpectedly passed: " + ", ".join(unexpected_passes)
            )
        print(f"Validated {len(selected)} failing benchmark baseline(s).")
        return 0
    if args.list or args.validate:
        for loaded in selected:
            suites = ",".join(sorted(loaded.task.suites))
            print(
                f"{loaded.task.id}\t{loaded.task.category}\t{suites}\t{loaded.task.title}"
            )
        print(f"Validated {len(selected)} benchmark task(s).")
        return 0

    output = args.output.resolve()
    config = get_config()
    run_config = _benchmark_run_config(config, args.temperature)
    runner = BenchmarkRunner(
        CodeRookBenchmarkExecutor(config, temperature=args.temperature),
        evidence_root=output / "evidence",
    )
    report = await runner.run(
        selected,
        repository_commit=_repository_commit(root),
        suite=args.suite,
        run_config=run_config,
    )
    require_candidate_contract(report)
    write_json_report(report, output / "report.json")
    write_markdown_report(report, output / "report.md")
    print(f"Benchmark pass@1: {report.passed}/{report.total} ({report.pass_rate:.1%})")
    print(f"Reports: {output}")
    return 0 if args.report_only or report.passed == report.total else 1


# 解析参数并进入异步主流程
def main() -> int:
    args = _build_parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    return asyncio.run(_run(args, root))


if __name__ == "__main__":
    raise SystemExit(main())

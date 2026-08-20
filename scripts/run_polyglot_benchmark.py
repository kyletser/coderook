#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

from code_rook.benchmark.executor import CodeRookBenchmarkExecutor
from code_rook.benchmark.models import BenchmarkRunConfig
from code_rook.benchmark.polyglot import load_polyglot_tasks
from code_rook.benchmark.report import write_json_report, write_markdown_report
from code_rook.benchmark.runner import BenchmarkRunner
from code_rook.core.config import get_config
from code_rook.core.llm.route_registry import RouteRegistry


# 定义固定 commit、隔离容器和小样本优先的 Polyglot benchmark 参数
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run CodeRook pass@1 on a pinned Aider Polyglot checkout."
    )
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument(
        "--agent-commit",
        default=os.environ.get("CODEROOK_SOURCE_COMMIT"),
        help="CodeRook source commit; required when .git is absent in the container.",
    )
    parser.add_argument("--aider-benchmark-dir", type=Path)
    parser.add_argument("--language", action="append", default=[])
    parser.add_argument("--keyword", action="append", default=[])
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--wall-time", type=float, default=600.0)
    parser.add_argument("--max-cost", type=float)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/aider-polyglot"),
    )
    return parser


# 读取当前 CodeRook commit，确保公开报告能回溯 Agent 代码版本
def _repository_commit(root: Path, explicit: str | None) -> str:
    if explicit and explicit != "unknown":
        return explicit
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    commit = result.stdout.strip() if result.returncode == 0 else ""
    if not commit:
        raise SystemExit("Cannot determine CodeRook commit; pass --agent-commit.")
    return commit


# 构造不含凭据且同时绑定 route 与公开数据集的报告配置
def _run_config(
    temperature: float,
    dataset_commit: str,
) -> BenchmarkRunConfig:
    config = get_config()
    route = RouteRegistry(config.llm, temperature_override=temperature).route()
    material = {
        "route_id": route.id,
        "provider": route.provider,
        "wire_format": route.wire_format,
        "base_url": str(route.base_url),
        "model": route.model,
        "thinking": route.thinking,
        "temperature": route.temperature,
        "router": config.llm.router,
        "dataset_commit": dataset_commit,
    }
    fingerprint = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return BenchmarkRunConfig(
        route_id=route.id,
        model=route.model,
        wire_format=route.wire_format,
        router=config.llm.router,
        thinking=route.thinking,
        temperature=route.temperature,
        config_fingerprint=fingerprint,
        benchmark_name="aider-polyglot-pass@1",
        dataset_name="Aider-AI/polyglot-benchmark",
        dataset_commit=dataset_commit,
    )


# 在明确的容器边界内运行固定切片并写出统一 benchmark 证据
async def _run(args: argparse.Namespace, root: Path) -> int:
    if os.environ.get("CODEROOK_BENCHMARK_CONTAINER") != "1":
        raise SystemExit(
            "Refusing to execute model-generated code on the host. "
            "Run inside a disposable container and set CODEROOK_BENCHMARK_CONTAINER=1."
        )
    dataset = args.dataset.resolve()
    tasks = load_polyglot_tasks(
        dataset,
        expected_commit=args.expected_commit,
        languages=set(args.language) or None,
        keywords=args.keyword or None,
        limit=args.limit,
        aider_benchmark_dir=(
            args.aider_benchmark_dir.resolve() if args.aider_benchmark_dir else None
        ),
        max_steps=args.max_steps,
        wall_time_s=args.wall_time,
        max_cost_usd=args.max_cost,
    )
    output = args.output.resolve()
    config = get_config()
    runner = BenchmarkRunner(
        CodeRookBenchmarkExecutor(config, temperature=args.temperature),
        evidence_root=output / "evidence",
    )
    report = await runner.run(
        tasks,
        repository_commit=_repository_commit(root, args.agent_commit),
        suite="aider-polyglot-pass@1",
        run_config=_run_config(args.temperature, args.expected_commit),
    )
    write_json_report(report, output / "report.json")
    write_markdown_report(report, output / "report.md")
    print(f"Aider Polyglot pass@1: {report.passed}/{report.total} ({report.pass_rate:.1%})")
    print(f"Reports: {output}")
    return 0 if report.passed == report.total else 1


# 解析命令行并进入异步公开 benchmark 执行流
def main() -> int:
    args = _build_parser().parse_args()
    root = Path(__file__).resolve().parents[1]
    return asyncio.run(_run(args, root))


if __name__ == "__main__":
    raise SystemExit(main())

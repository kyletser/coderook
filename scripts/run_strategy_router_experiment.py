from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from code_rook.benchmark.experiment import (
    candidate_git_state,
    configure_experiment_budget,
    resolve_experiment_candidate,
)
from code_rook.core.config import get_config
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.factory import create_provider_for_route
from code_rook.core.llm.pricing import estimate_cost, resolve_pricing_quote
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.llm.types import LlmResponse
from code_rook.core.strategy import TaskRisk, TaskStrategyRouter


class _CountingProvider:
    # 包装真实 Provider 并累计分类调用的 token 与可验证成本
    def __init__(self, provider: LLMProvider, model: str) -> None:
        self._provider = provider
        self._model = model
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0

    # 转发一次模型调用并从统一 UsageStats 累计真实用量
    async def chat(self, *args: Any, **kwargs: Any) -> LlmResponse:
        response = await self._provider.chat(*args, **kwargs)
        if response.usage is not None:
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
            self.cache_read_tokens += response.usage.cache_read_input_tokens
            self.cache_write_tokens += response.usage.cache_creation_input_tokens
        return response

    # 按仓库内置或用户覆盖价格返回当前累计美元成本
    def cost_usd(self) -> float | None:
        quote = resolve_pricing_quote(self._model)
        if quote is None:
            return None
        return estimate_cost(
            quote.pricing,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
        )


# 解析三种路由对照、重复次数和硬预算参数
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate CodeRook task strategy routing")
    parser.add_argument(
        "--method",
        action="append",
        choices=("rules_only", "llm_only", "hybrid"),
        default=[],
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--task-limit", type=int, default=12)
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument(
        "--tasks",
        type=Path,
        default=Path("benchmarks/tasks"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/reliability/task-router"),
    )
    return parser


# 从现有 50 任务类别和允许工具生成冻结的预期标签
def _expected(task: dict[str, Any]) -> dict[str, object]:
    category = str(task["category"])
    intent = {
        "explain": "explain",
        "single_file_fix": "fix",
        "multi_file_change": "multi_file_change",
        "test_and_verify": "test",
        "refactor": "refactor",
        "security_negative": "inspect",
    }[category]
    scope = {
        "explain": "read_only",
        "security_negative": "read_only",
        "single_file_fix": "single_file",
        "test_and_verify": "single_file",
        "refactor": "multi_file",
        "multi_file_change": "multi_file",
    }[category]
    allowed = {str(value).casefold() for value in task.get("allowed_tools", [])}
    if any(value in allowed for value in {"web_fetch", "web_search"}):
        risk = "external"
    elif any(value in allowed for value in {"bash", "run", "run_tests", "run_verifiers"}):
        risk = "shell"
    elif scope == "read_only":
        risk = "read"
    else:
        risk = "mutate"
    return {
        "intent": intent,
        "scope": scope,
        "risk": risk,
        "delegation_allowed": category in {"multi_file_change", "refactor"},
    }


# 对每个类别计算 one-vs-rest F1 后返回宏平均
def _macro_f1(expected: list[str], actual: list[str]) -> float:
    labels = sorted(set(expected) | set(actual))
    scores: list[float] = []
    for label in labels:
        true_positive = sum(e == label and a == label for e, a in zip(expected, actual))
        false_positive = sum(e != label and a == label for e, a in zip(expected, actual))
        false_negative = sum(e == label and a != label for e, a in zip(expected, actual))
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return sum(scores) / len(scores) if scores else 0.0


# 返回完整 Git commit，避免报告绑定到不可追溯的工作树缩写
def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 对任务目录内容计算稳定数据集指纹
def _dataset_fingerprint(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


# 按任务类别轮询选择小规模诊断样本，避免目录顺序造成类别偏置
def _stratified_paths(paths: list[Path], limit: int) -> list[Path]:
    groups: dict[str, list[Path]] = {}
    for path in paths:
        task = json.loads(path.read_text(encoding="utf-8"))
        groups.setdefault(str(task["category"]), []).append(path)
    selected: list[Path] = []
    while len(selected) < limit:
        advanced = False
        for category in sorted(groups):
            if groups[category] and len(selected) < limit:
                selected.append(groups[category].pop(0))
                advanced = True
        if not advanced:
            break
    return selected


# 运行完整方法与重复矩阵，在完整重复边界检查成本并保留所有原始结果
async def _run(
    args: argparse.Namespace,
    *,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    methods = args.method or ["rules_only"]
    all_paths = sorted(args.tasks.glob("*.json"))
    if len(all_paths) != 50:
        raise SystemExit(f"expected 50 frozen tasks, found {len(all_paths)}")
    paths = _stratified_paths(all_paths, args.task_limit)
    tasks = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    config = get_config()
    resolved = RouteRegistry(config.llm, temperature_override=0.0).resolve()
    uses_model = any(method != "rules_only" for method in methods)
    provider = (
        _CountingProvider(
            create_provider_for_route(resolved.route, resolved.credential),
            resolved.route.model,
        )
        if uses_model
        else None
    )
    router = TaskStrategyRouter()
    rows: list[dict[str, Any]] = []
    completed_blocks: list[str] = []
    last_block_cost = 0.0
    for method in methods:
        for repeat in range(1, args.repeats + 1):
            spent = (provider.cost_usd() or 0.0) if provider is not None else 0.0
            if spent >= args.max_cost_usd or (
                last_block_cost > 0 and spent + last_block_cost > args.max_cost_usd
            ):
                break
            block_start_cost = spent
            for task in tasks:
                started = time.perf_counter()
                profile = await router.classify(
                    str(task["goal"]),
                    provider=provider,
                    method=method,
                )
                expected = _expected(task)
                rows.append(
                    {
                        "method": method,
                        "repeat": repeat,
                        "task_id": task["id"],
                        "expected": expected,
                        "actual": profile.model_dump(mode="json"),
                        "latency_ms": round((time.perf_counter() - started) * 1_000, 3),
                    }
                )
            completed_blocks.append(f"{method}:{repeat}")
            last_block_cost = max(
                0.0,
                ((provider.cost_usd() or 0.0) if provider is not None else 0.0)
                - block_start_cost,
            )
    summaries: dict[str, Any] = {}
    for method in methods:
        selected = [row for row in rows if row["method"] == method]
        expected_intents = [str(row["expected"]["intent"]) for row in selected]
        actual_intents = [str(row["actual"]["intent"]) for row in selected]
        risk_false_negatives = sum(
            list(TaskRisk).index(TaskRisk(str(row["actual"]["risk"])))
            < list(TaskRisk).index(TaskRisk(str(row["expected"]["risk"])))
            for row in selected
        )
        delegation_correct = sum(
            bool(row["actual"]["delegation_allowed"]) == bool(row["expected"]["delegation_allowed"])
            for row in selected
        )
        summaries[method] = {
            "samples": len(selected),
            "macro_f1": _macro_f1(expected_intents, actual_intents),
            "risk_false_negatives": risk_false_negatives,
            "delegation_accuracy": delegation_correct / len(selected) if selected else 0.0,
            "latency_ms_p50": (
                sorted(float(row["latency_ms"]) for row in selected)[len(selected) // 2]
                if selected
                else None
            ),
            "source_counts": dict(Counter(str(row["actual"]["source"]) for row in selected)),
        }
    return {
        "schema_version": 1,
        "experiment": "task-strategy-router-diagnostic",
        "effectiveness_claim": False,
        "commit": _commit(),
        "dataset_fingerprint": _dataset_fingerprint(paths),
        "route": candidate,
        "budget": {
            "limit_usd": args.max_cost_usd,
            "spent_usd": provider.cost_usd() if provider is not None else 0.0,
            "input_tokens": provider.input_tokens if provider is not None else 0,
            "output_tokens": provider.output_tokens if provider is not None else 0,
        },
        "requested_repeats": args.repeats,
        "completed_blocks": completed_blocks,
        "summaries": summaries,
        "rows": rows,
    }


# 将聚合指标和诚实门禁结果渲染为简洁 Markdown
def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Task Strategy Router Category-Proxy Diagnostic",
        "",
        f"Commit: `{report['commit']}`",
        f"Dataset: `{report['dataset_fingerprint']}`",
        "",
        "| Method | Samples | Macro-F1 | Risk FN | Delegation accuracy |",
        "|---|---:|---:|---:|---:|",
    ]
    for method, summary in report["summaries"].items():
        lines.append(
            f"| {method} | {summary['samples']} | {summary['macro_f1']:.3f} | "
            f"{summary['risk_false_negatives']} | {summary['delegation_accuracy']:.1%} |"
        )
    lines.extend(
        [
            "",
            "Numbers above are computed from every retained raw row; no best run is selected.",
            "This is a routing diagnostic, not evidence that coding task outcomes improved.",
            "Expected labels are benchmark-category proxies and must not be reported as user-intent accuracy.",
        ]
    )
    return "\n".join(lines) + "\n"


# 解析参数、执行实验并同时写出原始 JSON 与聚合 Markdown
def main() -> int:
    args = _parser().parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    if args.max_cost_usd <= 0 or args.max_cost_usd > 35:
        raise SystemExit("--max-cost-usd must be in (0, 35]")
    if not 1 <= args.task_limit <= 50:
        raise SystemExit("--task-limit must be between 1 and 50")
    methods = args.method or ["rules_only"]
    uses_model = any(method != "rules_only" for method in methods)
    if uses_model and not args.allow_model_calls:
        raise SystemExit(
            "llm_only/hybrid diagnostics require explicit --allow-model-calls"
        )
    try:
        _resolved, candidate = resolve_experiment_candidate(
            get_config(),
            temperature=0.0,
        )
    except RuntimeError as exc:
        raise SystemExit(f"experiment preflight failed: {exc}") from exc
    git_state = candidate_git_state()
    preflight = {
        "candidate": candidate,
        "git": git_state,
        "methods": methods,
        "task_limit": args.task_limit,
        "repeats": args.repeats,
        "maximum_model_calls": (
            args.task_limit
            * args.repeats
            * sum(method != "rules_only" for method in methods)
        ),
        "max_cost_usd": args.max_cost_usd,
        "model_calls": False,
        "effectiveness_claim": False,
    }
    if args.preflight:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0 if git_state["working_tree_clean"] else 2
    if not git_state["working_tree_clean"]:
        raise SystemExit("experiment requires a clean Git working tree")
    args.output.mkdir(parents=True, exist_ok=True)
    if uses_model:
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
    print(f"Task router evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

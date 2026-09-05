from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from code_rook.benchmark.experiment import (
    candidate_git_state,
    configure_experiment_budget,
    resolve_experiment_candidate,
)
from code_rook.core.compact.compactor import Compactor
from code_rook.core.compact.protocol import estimate_messages_tokens
from code_rook.core.config import get_config
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.factory import create_provider_for_route
from code_rook.core.llm.pricing import estimate_cost, resolve_pricing_quote
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.llm.types import LlmResponse
from code_rook.core.session.store import SessionStore


class _MeteredProvider:
    # 包装压缩和探针共用 Provider，累计真实 token 与价格证据
    def __init__(self, provider: LLMProvider, model: str) -> None:
        self._provider = provider
        self._model = model
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0

    # 转发模型调用并累加统一 usage 统计
    async def chat(self, *args: Any, **kwargs: Any) -> LlmResponse:
        response = await self._provider.chat(*args, **kwargs)
        if response.usage is not None:
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
            self.cache_read_tokens += response.usage.cache_read_input_tokens
            self.cache_write_tokens += response.usage.cache_creation_input_tokens
        return response

    # 依据绑定模型的价格表计算当前累计成本
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


# 生成含过期错误、重复读取、大输出和完整工具闭环的冻结长历史
def _history(task: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": f"Work on {task['id']} reliably."},
        {"role": "assistant", "content": "I will inspect evidence before changing code."},
        {
            "role": "user",
            "content": "Constraints: " + " | ".join(task["constraints"]),
        },
        {
            "role": "assistant",
            "content": f"Recorded target path {task['path']} and the constraints.",
        },
    ]
    repeated = (f"FILE {task['path']}\n" + "implementation detail\n" * 200)[
        : int(task["large_output_chars"])
    ]
    rounds = int(task["history_rounds"])
    for index in range(rounds):
        tool_id = f"{task['id']}-read-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "File",
                            "input": {"action": "read", "path": task["path"]},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": repeated,
                        }
                    ],
                },
                {
                    "role": "assistant",
                    "content": (
                        f"Round {index}: current evidence retained. "
                        + (
                            f"Stale error: {task['stale_error']}"
                            if index == 1
                            else "Continue with the current evidence."
                        )
                    ),
                },
            ]
        )
    messages.extend(
        [
            {"role": "user", "content": "The stale error is obsolete; use current evidence."},
            {"role": "assistant", "content": "Understood; the original constraints still apply."},
        ]
    )
    return messages


# 从探针文本判断路径和三项固定约束是否全部可恢复
def _probe_passed(text: str, task: dict[str, Any]) -> bool:
    lowered = text.casefold()
    required = [str(task["path"]), *[str(value) for value in task["constraints"]]]
    return all(value.casefold() in lowered for value in required)


# 返回报告绑定的完整 Git commit
def _commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 按数据集声明的 Pilot 顺序选择任务并保持扩容时样本稳定
def _select_tasks(dataset: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    tasks = list(dataset["tasks"])
    by_id = {str(task["id"]): task for task in tasks}
    pilot_ids = [str(value) for value in dataset.get("pilot_task_ids", [])]
    ordered = [by_id[task_id] for task_id in pilot_ids if task_id in by_id]
    ordered.extend(task for task in tasks if task not in ordered)
    return ordered[:limit]


# 运行三种压缩策略的完整对照并在组边界执行预算停止
async def _run(
    args: argparse.Namespace,
    *,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    dataset_bytes = args.dataset.read_bytes()
    dataset = json.loads(dataset_bytes)
    all_tasks = dataset["tasks"]
    if len(all_tasks) != 12:
        raise SystemExit(f"expected 12 long-context scenarios, found {len(all_tasks)}")
    tasks = _select_tasks(dataset, args.task_limit)
    config = get_config()
    resolved = RouteRegistry(config.llm, temperature_override=0.0).resolve()
    provider = _MeteredProvider(
        create_provider_for_route(resolved.route, resolved.credential),
        resolved.route.model,
    )
    rows: list[dict[str, Any]] = []
    completed_blocks: list[str] = []
    last_block_cost = 0.0
    strategies = ("truncate", "structured", "adaptive_evidence")
    with tempfile.TemporaryDirectory(prefix="coderook-compaction-experiment-") as raw_tmp:
        root = Path(raw_tmp)
        for strategy in strategies:
            for repeat in range(1, args.repeats + 1):
                spent = provider.cost_usd() or 0.0
                if spent >= args.max_cost_usd or (
                    last_block_cost > 0 and spent + last_block_cost > args.max_cost_usd
                ):
                    break
                block_start = spent
                for task_index, task in enumerate(tasks):
                    session_id = f"sess-{strategy.replace('_', '-')}-{repeat}-{task_index}"
                    store = SessionStore(root / "sessions")
                    goal_text = f"Target {task['path']}. Constraints: " + " | ".join(
                        task["constraints"]
                    )
                    store.append_session_event(
                        session_id,
                        event_type="input.admitted",
                        turn_id="experiment",
                        payload={"role": "user", "content": goal_text},
                    )
                    store.append_session_event(
                        session_id,
                        event_type="task.profiled",
                        turn_id="experiment",
                        payload={
                            "profile": {
                                "context_policy": "long_task",
                                "strategy": "plan_first",
                            }
                        },
                    )
                    messages = _history(task)
                    started = time.perf_counter()
                    input_tokens_before = provider.input_tokens
                    output_tokens_before = provider.output_tokens
                    compactor = Compactor(
                        EventBus(),
                        store.session_dir(session_id),
                        session_id,
                        store=store,
                        retain_ratio=0.25,
                        strategy=strategy,
                    )
                    result = await compactor.compact_messages(messages, provider)
                    effective = result.messages if result is not None else messages
                    probe = await provider.chat(
                        messages=[
                            *effective,
                            {
                                "role": "user",
                                "content": (
                                    "Return the exact target path and all non-negotiable "
                                    "constraints from this task. Be concise."
                                ),
                            },
                        ],
                        tool_schemas=[],
                        bus=EventBus(),
                        run_id=f"probe-{session_id}",
                        step=0,
                        system="Answer only from the supplied conversation evidence.",
                        thinking="off",
                    )
                    rendered = json.dumps(effective, ensure_ascii=False)
                    retained_facts = sum(
                        str(value).casefold() in rendered.casefold()
                        for value in [task["path"], *task["constraints"]]
                    )
                    fact_total = 1 + len(task["constraints"])
                    rows.append(
                        {
                            "strategy": strategy,
                            "repeat": repeat,
                            "task_id": task["id"],
                            "category": task["category"],
                            "passed": _probe_passed(probe.text, task),
                            "fallback_to_original": result is None,
                            "original_tokens": estimate_messages_tokens(messages),
                            "effective_tokens": estimate_messages_tokens(effective),
                            "compression_ratio": (
                                estimate_messages_tokens(effective)
                                / estimate_messages_tokens(messages)
                            ),
                            "fact_retention": retained_facts / fact_total,
                            "pinned_fact_count": (
                                result.pinned_fact_count if result is not None else 0
                            ),
                            "deduplicated_reads": (
                                result.deduplicated_reads if result is not None else 0
                            ),
                            "latency_ms": round(
                                (time.perf_counter() - started) * 1_000,
                                3,
                            ),
                            "probe_text": probe.text[:2_000],
                            "model_input_tokens": (
                                provider.input_tokens - input_tokens_before
                            ),
                            "model_output_tokens": (
                                provider.output_tokens - output_tokens_before
                            ),
                        }
                    )
                completed_blocks.append(f"{strategy}:{repeat}")
                last_block_cost = max(0.0, (provider.cost_usd() or 0.0) - block_start)
    summaries: dict[str, Any] = {}
    for strategy in strategies:
        selected = [row for row in rows if row["strategy"] == strategy]
        ratios = sorted(float(row["compression_ratio"]) for row in selected)
        summaries[strategy] = {
            "tasks": len(selected),
            "passed": sum(bool(row["passed"]) for row in selected),
            "pass_rate": (
                sum(bool(row["passed"]) for row in selected) / len(selected) if selected else 0.0
            ),
            "fact_retention_rate": (
                sum(float(row["fact_retention"]) for row in selected) / len(selected)
                if selected
                else 0.0
            ),
            "median_input_reduction": (1.0 - ratios[len(ratios) // 2] if ratios else 0.0),
            "fallbacks": sum(bool(row["fallback_to_original"]) for row in selected),
            "duplicate_reads_removed": sum(int(row["deduplicated_reads"]) for row in selected),
            "median_model_input_tokens": (
                sorted(int(row["model_input_tokens"]) for row in selected)[
                    len(selected) // 2
                ]
                if selected
                else 0
            ),
        }
    return {
        "schema_version": 1,
        "experiment": "evidence-preserving-compaction",
        "synthetic_data": True,
        "commit": _commit(),
        "dataset_fingerprint": hashlib.sha256(dataset_bytes).hexdigest(),
        "route": candidate,
        "budget": {
            "limit_usd": args.max_cost_usd,
            "spent_usd": provider.cost_usd(),
            "input_tokens": provider.input_tokens,
            "output_tokens": provider.output_tokens,
        },
        "requested_repeats": args.repeats,
        "completed_blocks": completed_blocks,
        "summaries": summaries,
        "rows": rows,
    }


# 将三策略真实指标表渲染为不挑选最好一次的 Markdown 报告
def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Evidence-Preserving Compaction Experiment",
        "",
        "> Dataset is synthetic and frozen; it is not presented as real user traffic.",
        "",
        "| Strategy | Tasks | Recall pass | Fact retention | Median context reduction | Median model input | Fallbacks |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for strategy, summary in report["summaries"].items():
        lines.append(
            f"| {strategy} | {summary['tasks']} | {summary['pass_rate']:.1%} | "
            f"{summary['fact_retention_rate']:.1%} | "
            f"{summary['median_input_reduction']:.1%} | "
            f"{summary['median_model_input_tokens']} | {summary['fallbacks']} |"
        )
    lines.extend(["", "All repeats and failure samples remain in `report.json`."])
    return "\n".join(lines) + "\n"


# 解析低成本 Pilot 和显式完整实验参数
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run long-context compaction ablations")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/reliability/long_context_tasks.json"),
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--task-limit", type=int, default=6)
    parser.add_argument("--max-cost-usd", type=float, default=2.0)
    parser.add_argument("--expected-model")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/reliability/compaction"),
    )
    return parser


# 解析实验参数并在零调用预检通过后写入原始 JSON 与聚合 Markdown
def main() -> int:
    args = _parser().parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    if not 1 <= args.task_limit <= 12:
        raise SystemExit("--task-limit must be between 1 and 12")
    if args.max_cost_usd <= 0 or args.max_cost_usd > 35:
        raise SystemExit("--max-cost-usd must be in (0, 35]")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    selected = _select_tasks(dataset, args.task_limit)
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
        "task_ids": [task["id"] for task in selected],
        "repeats": args.repeats,
        "maximum_model_calls": len(selected) * args.repeats * 5,
        "max_cost_usd": args.max_cost_usd,
        "model_calls": False,
    }
    if args.preflight:
        print(json.dumps(preflight, ensure_ascii=False, indent=2))
        return 0 if git_state["working_tree_clean"] else 2
    if not git_state["working_tree_clean"]:
        raise SystemExit("experiment requires a clean Git working tree")
    args.output.mkdir(parents=True, exist_ok=True)
    configure_experiment_budget(
        args.output,
        limit_usd=args.max_cost_usd,
        expected_model=str(candidate["model"]),
    )
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
    print(f"Compaction evidence: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

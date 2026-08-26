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


# 运行三种压缩策略的两次完整对照并在组边界执行预算停止
async def _run(args: argparse.Namespace) -> dict[str, Any]:
    dataset_bytes = args.dataset.read_bytes()
    dataset = json.loads(dataset_bytes)
    tasks = dataset["tasks"]
    if len(tasks) != 12:
        raise SystemExit(f"expected 12 long-context scenarios, found {len(tasks)}")
    config = get_config()
    resolved = RouteRegistry(config.llm, temperature_override=0.0).resolve()
    if resolved.route.id != "legacy-anthropic" or resolved.route.model != "deepseek-v4-flash":
        raise SystemExit("experiment requires route legacy-anthropic / deepseek-v4-flash")
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
        }
    return {
        "schema_version": 1,
        "experiment": "evidence-preserving-compaction",
        "synthetic_data": True,
        "commit": _commit(),
        "dataset_fingerprint": hashlib.sha256(dataset_bytes).hexdigest(),
        "route": {
            "id": resolved.route.id,
            "model": resolved.route.model,
            "wire_format": resolved.route.wire_format,
            "temperature": resolved.route.temperature,
        },
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
        "| Strategy | Tasks | Pass@1 | Fact retention | Median token reduction | Fallbacks |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for strategy, summary in report["summaries"].items():
        lines.append(
            f"| {strategy} | {summary['tasks']} | {summary['pass_rate']:.1%} | "
            f"{summary['fact_retention_rate']:.1%} | "
            f"{summary['median_input_reduction']:.1%} | {summary['fallbacks']} |"
        )
    lines.extend(["", "All repeats and failure samples remain in `report.json`."])
    return "\n".join(lines) + "\n"


# 解析实验参数并写入原始 JSON 与聚合 Markdown
def main() -> int:
    parser = argparse.ArgumentParser(description="Run long-context compaction ablations")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("benchmarks/reliability/long_context_tasks.json"),
    )
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-cost-usd", type=float, default=35.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark-results/reliability/compaction"),
    )
    args = parser.parse_args()
    if args.repeats < 1:
        raise SystemExit("--repeats must be positive")
    if args.max_cost_usd <= 0 or args.max_cost_usd > 35:
        raise SystemExit("--max-cost-usd must be in (0, 35]")
    report = asyncio.run(_run(args))
    args.output.mkdir(parents=True, exist_ok=True)
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

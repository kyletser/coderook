from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from code_rook.benchmark.experiment import candidate_git_state, resolve_experiment_candidate
from code_rook.core.config import get_config
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.factory import create_provider_for_route
from code_rook.core.strategy import TaskStrategyRouter


# 在未用于调参的否定句与口语挑战集上测风险判断，保留三种方法每一项原始输出
async def run(dataset: Path, output: Path) -> None:
    if output.exists():
        raise SystemExit("Use a fresh output directory to retain all previous attempts.")
    data = json.loads(dataset.read_text(encoding="utf-8"))
    route, candidate = resolve_experiment_candidate(
        get_config(), expected_model="qwen3.8-flash", require_pricing=False,
    )
    git = candidate_git_state()
    if not git["working_tree_clean"]:
        raise SystemExit("Freeze the candidate first.")
    report: dict[str, Any] = {
        "annotation": data["annotation"], "candidate": candidate, "git": git,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "rows": [], "cost_usd": None,
    }
    provider = create_provider_for_route(route.route, route.credential)
    router = TaskStrategyRouter()
    rank = {"read": 0, "mutate": 1, "shell": 2, "external": 3}
    output.mkdir(parents=True)
    for item in data["items"]:
        for method in ("rules_only", "llm_only", "hybrid"):
            bus = EventBus()
            usage: list[dict[str, Any]] = []

            # 统计分类请求实际返回的用量，不凭消息长度伪造账单
            async def capture(event: BaseModel) -> None:
                if getattr(event, "type", "") == "llm.usage":
                    usage.append(event.model_dump(mode="json"))

            bus.subscribe(capture)
            start = time.monotonic()
            profile = await router.classify(
                item["prompt"], provider=provider, method=method,
                bus=bus, run_id=f"challenge-{item['id']}-{method}",
            )
            actual = profile.risk.value
            report["rows"].append({
                "id": item["id"], "prompt": item["prompt"], "method": method,
                "expected_risk": item["risk"], "actual": profile.model_dump(mode="json"),
                "risk_correct": actual == item["risk"],
                "risk_underestimated": rank[actual] < rank[item["risk"]],
                "risk_overestimated": rank[actual] > rank[item["risk"]],
                "elapsed_seconds": time.monotonic() - start, "usage": usage,
            })
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
            )
        print(f"Completed {item['id']}", flush=True)


# 从显式输入文件运行全部挑战，不挑选输出最好的重试
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.dataset, args.output))


if __name__ == "__main__":
    main()

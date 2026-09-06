from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from code_rook.benchmark.experiment import candidate_git_state, resolve_experiment_candidate
from code_rook.core.authority import AuthorityProfile, AuthoritySnapshot, WorkspaceTrust
from code_rook.core.compact.compactor import Compactor
from code_rook.core.compact.protocol import estimate_messages_tokens, validate_tool_protocol
from code_rook.core.config import get_config
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.factory import create_provider_for_route
from code_rook.core.llm.route_registry import RouteRegistry
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runner import AgentRunner
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.strategy import TaskStrategy

POSITIVE_CONTROLS = {
    "json-record": """import json
def parse_record(text):
    try:
        result = json.loads(text)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError('E_RECORD') from None
""",
    "stable-unique": """def stable_unique(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
""",
    "duration": """import re
from decimal import Decimal
def parse_duration(text):
    match = re.fullmatch(r'(\\d+(?:\\.\\d+)?)(ms|s)', text.strip())
    if match is None:
        raise ValueError('E_DURATION')
    number = Decimal(match[1]) * (1000 if match[2] == 's' else 1)
    if number != number.to_integral_value():
        raise ValueError('E_DURATION')
    return int(number)
""",
    "settings-update": """def update_settings(current, changes):
    if any(key not in current for key in changes):
        raise ValueError('E_SETTING')
    return {**current, **changes}
""",
}


# 在 Agent 已退出后把候选代码复制到独立目录验证，验收脚本从不进入 Agent 工作区
def verify(source: str, verifier: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="coderook-external-verifier-") as raw:
        root = Path(raw)
        (root / "target.py").write_text(source, encoding="utf-8")
        (root / "verify.py").write_text(verifier, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "verify.py"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "output": (result.stdout + result.stderr).replace(str(root), "<verifier>"),
        }


# 验证每个坏实现能过可见烟测但不过验收，正确实现能过验收，防止模型调用浪费在坏题上
def preflight(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for task in tasks:
        row = {
            "id": task["id"],
            "baseline_smoke": verify(task["source"], task["smoke"]),
            "baseline_verifier": verify(task["source"], task["verifier"]),
            "positive_verifier": verify(POSITIVE_CONTROLS[task["id"]], task["verifier"]),
        }
        rows.append(row)
        if not (
            row["baseline_smoke"]["passed"]
            and not row["baseline_verifier"]["passed"]
            and row["positive_verifier"]["passed"]
        ):
            raise RuntimeError(json.dumps(row, ensure_ascii=False))
    return rows


# 构造冻结的真实源码读取历史，业务要求只在早期给出，最后请求不重复约束
def history(task: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": task["goal"] + "\nConstraints: " + "\n".join(task["constraints"]),
        },
        {
            "role": "assistant",
            "content": "I will preserve the API and requirements while fixing target.py.",
        },
    ]
    # 同一冗长工具结果反复出现，模拟重复查看完整测试输出；明确标记为合成日志。
    log = "Synthetic historical smoke log; NOT a requirements test.\n" + "\n".join(
        f"case-{index:03d}: smoke passed, elapsed=0.001s" for index in range(140)
    )
    for index in range(task["history_rounds"]):
        messages.append(
            {
                "role": "user",
                "content": "Continue inspecting the current implementation before editing.",
            }
        )
        for suffix, name, params, output in (
            ("source", "File", {"action": "read", "path": "target.py"}, task["source"]),
            ("log", "File", {"action": "read", "path": "historical-smoke.log"}, log),
        ):
            call = f"history-{index}-{suffix}"
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": call, "name": name, "input": params}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": call, "content": output}
                        ],
                    },
                ]
            )
        messages.append(
            {
                "role": "assistant",
                "content": "The smoke passes, but the reported behavior is still unfixed.",
            }
        )
    messages.extend(
        [
            {
                "role": "user",
                "content": "The previous environment error was resolved; do not chase it again.",
            },
            {"role": "assistant", "content": "Ready to implement the previously requested fix."},
        ]
    )
    valid, errors = validate_tool_protocol(messages)
    if not valid:
        raise ValueError(errors)
    return messages


# 写入可复核 JSON，不输出凭据或本机用户路径
def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# 用生产压缩器和真实 AgentRunner 继续编码，计入摘要及后续全部 usage 事件
async def trial(task: dict[str, Any], policy: str, output: Path) -> dict[str, Any]:
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    bus = EventBus()

    # 保留模型调用、工具和压缩轨迹以便审计，最终公开前另行脱敏
    async def capture(event: BaseModel) -> None:
        events.append(event.model_dump(mode="json"))

    bus.subscribe(capture)
    config = copy.deepcopy(get_config())
    config.agent.max_steps = 12
    config.agent.max_step_continues = 0
    routes = RouteRegistry(config.llm, temperature_override=0.0)
    resolved = routes.resolve()
    provider = create_provider_for_route(resolved.route, resolved.credential)
    with tempfile.TemporaryDirectory(prefix="coderook-compaction-coding-") as raw:
        root = Path(raw)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "target.py").write_text(task["source"], encoding="utf-8")
        (workspace / "smoke.py").write_text(task["smoke"], encoding="utf-8")
        store = SessionStore(root / "sessions")
        sid = "sess-compaction-coding"
        session = Session(
            sid,
            "chat",
            "active",
            task["id"],
            "2026-09-06T00:00:00Z",
            "2026-09-06T00:00:00Z",
            workspace=str(workspace),
        )
        store.write_meta(session)
        original = history(task)
        store.append_messages(sid, original, run_id="synthetic-history")
        ledger = store.session_dir(sid) / "thread.jsonl"
        prefix = ledger.read_bytes()
        compactor = Compactor(bus, store.session_dir(sid), sid, store=store, strategy=policy)
        result = await compactor.compact_messages(original, provider, run_id="summary")
        if result is not None:
            await compactor.commit(result, run_id="summary", trigger="experiment")
        projected = store.read_messages(sid)
        assert ledger.read_bytes().startswith(prefix), "compaction rewrote the ledger"
        assert SessionStore(root / "sessions").read_messages(sid) == projected
        save(output / "context-before.json", original)
        save(output / "context-after.json", projected)
        summary_events = len(events)
        goal = (
            "Fix the previously reported issue in target.py, preserving our earlier requirements. "
            "Run python -B smoke.py to verify. Only change target.py. "
            "Do not ask for clarification; use the conversation history."
        )
        store.append_message(sid, "user", goal)
        permission = PermissionManager(timeout_s=0)
        permission.set_authority_snapshot(
            sid,
            AuthoritySnapshot(
                profile=AuthorityProfile.FULL_ACCESS,
                workspace_trust=WorkspaceTrust.TRUSTED,
            ),
        )
        allowed = ["File", "Bash", "Run"]
        permission.set_session_mode(sid, "allow_list", allow_tools=allowed)
        runner = AgentRunner(
            config,
            bus=bus,
            runs_dir=root / "runs",
            permission_manager=permission,
            workspace_root=workspace,
            route_registry=routes,
        )
        try:
            outcome = await asyncio.wait_for(
                runner.run_and_capture(
                    goal,
                    run_id="coding",
                    session=session,
                    store=store,
                    tool_whitelist=allowed,
                    resolved_route=resolved,
                    resolved_route_is_explicit=True,
                    strategy_override=TaskStrategy.DIRECT,
                ),
                timeout=240,
            )
            status, reason = outcome.status, outcome.reason
        except TimeoutError:
            status, reason = "failed", "experiment_wall_time_exceeded"
        final = (workspace / "target.py").read_text(encoding="utf-8")
        verification = verify(final, task["verifier"])
        smoke_intact = (workspace / "smoke.py").read_text(encoding="utf-8") == task["smoke"]
        output.mkdir(parents=True, exist_ok=True)
        (output / "target.py").write_text(final, encoding="utf-8")
        # 原始账本保持字节不变；路径脱敏只用于下面的派生事件导出，不能破坏 checksum 链。
        (output / "thread.jsonl").write_bytes(ledger.read_bytes())
        save(
            output / "events.json",
            json.loads(json.dumps(events).replace(json.dumps(str(root))[1:-1], "<trial>")),
        )
        usage = [event for event in events if event["type"] == "llm.usage"]
        summary_usage = [event for event in events[:summary_events] if event["type"] == "llm.usage"]
        row = {
            "id": task["id"],
            "policy": policy,
            "status": status,
            "reason": reason,
            "passed": status == "success" and verification["passed"] and smoke_intact,
            "verification": verification,
            "smoke_intact": smoke_intact,
            "compaction_applied": result is not None,
            "append_only": True,
            "projection_replay_equal": True,
            "before_estimated_tokens": estimate_messages_tokens(original),
            "after_estimated_tokens": estimate_messages_tokens(projected),
            "deduplicated_reads": result.deduplicated_reads if result else 0,
            "pinned_count": result.pinned_fact_count if result else 0,
            "pinned_retained": result.pinned_fact_retained if result else 0,
            "elapsed_seconds": time.monotonic() - started,
            "usage": usage,
            "summary_usage": summary_usage,
            "input_tokens": sum(event["input_tokens"] for event in usage),
            "output_tokens": sum(event["output_tokens"] for event in usage),
        }
        save(output / "result.json", row)
        return row


# 交错策略次序运行全部冻结任务，逐格落盘且不选择最好一次
async def run(
    args: argparse.Namespace, tasks: list[dict[str, Any]], report: dict[str, Any]
) -> None:
    for index, task in enumerate(tasks):
        policies = ["structured", "adaptive_evidence"]
        if index % 2:
            policies.reverse()
        for policy in policies:
            print(f"Running {task['id']}/{policy}", flush=True)
            row = await trial(task, policy, args.output / task["id"] / policy)
            report["rows"].append(row)
            save(args.output / "report.json", report)
            print(
                f"Finished: passed={row['passed']}, "
                f"tokens={row['input_tokens'] + row['output_tokens']}",
                flush=True,
            )


# 先做零费用正负对照，再绑定候选提交与数据集后启动真实模型实验
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    args = parser.parse_args()
    tasks = json.loads(args.dataset.read_text(encoding="utf-8"))["tasks"]
    controls = preflight(tasks)
    if args.preflight:
        print(json.dumps(controls, ensure_ascii=False, indent=2))
        return 0
    if args.output.exists():
        raise SystemExit("Use a new output directory; previous attempts must be retained.")
    _, candidate = resolve_experiment_candidate(
        get_config(), expected_model="qwen3.8-flash", require_pricing=False
    )
    git = candidate_git_state()
    if not git["working_tree_clean"]:
        raise SystemExit("Freeze the candidate commit before running.")
    report = {
        "experiment": "synthetic-compaction-coding",
        "candidate": candidate,
        "git": git,
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "controls": controls,
        "cost_usd": None,
        "rows": [],
    }
    save(args.output / "report.json", report)
    asyncio.run(run(args, tasks, report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

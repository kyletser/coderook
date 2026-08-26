from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

from pydantic import JsonValue, TypeAdapter

from code_rook.core.authority import AuthorityProfile
from code_rook.core.receipts.models import (
    RunOutcome,
    SandboxPlanReceipt,
    TurnApprovalCounts,
    TurnAuthorityReceipt,
    TurnFileChangeReceipt,
    TurnProcessUsageReceipt,
    TurnReceipt,
)
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)
from code_rook.core.sandbox.planner import SandboxTier, plan_sandbox, tier_for_auto_review

_TERMINAL = {TurnStatus.COMPLETED, TurnStatus.FAILED, TurnStatus.INTERRUPTED}
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_RUN_OUTCOMES = frozenset(
    {
        "completed",
        "tool_use",
        "length",
        "incomplete",
        "content_filtered",
        "failed",
        "cancelled",
        "transport_error",
    }
)


# 将持久 JSON 数值安全收窄为非负整数，布尔值和复杂对象按零处理
def _json_count(value: JsonValue | None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


# 将事件 payload 中的结构化对象按稳定键去重
def _unique_payloads(
    events: Iterable[RuntimeEventRecord],
    event_types: set[str],
    *,
    key: str,
) -> list[dict[str, JsonValue]]:
    found: dict[str, dict[str, JsonValue]] = {}
    for event in events:
        if event.type not in event_types:
            continue
        raw_key = event.payload.get(key)
        stable_key = str(raw_key) if raw_key is not None else f"seq:{event.seq}"
        found[stable_key] = dict(event.payload)
    return list(found.values())


# 判断工具调用是否属于旧写工具或 File family 的写 action
def _is_mutating_file_call(item: TurnItemRecord) -> bool:
    if item.kind != TurnItemKind.TOOL_CALL:
        return False
    tool_name = item.payload.get("tool_name")
    params = item.payload.get("params")
    if not isinstance(params, dict):
        params = {}
    if tool_name == "apply_patch" and params.get("dry_run") is True:
        return False
    if tool_name in {"apply_patch", "edit_file", "write_file"}:
        return True
    if tool_name != "File":
        return False
    action = params.get("action")
    if action == "patch" and params.get("dry_run") is True:
        return False
    return isinstance(action, str) and action in {"write", "edit", "patch"}


# 返回同时具备成功终态结果的变更工具调用 ID，排除失败、拒绝和中断调用
def _successful_mutating_call_ids(records: Iterable[TurnItemRecord]) -> set[str | None]:
    items = list(records)
    mutating = {
        item.tool_call_id for item in items if _is_mutating_file_call(item)
    }
    succeeded = {
        item.tool_call_id
        for item in items
        if item.kind == TurnItemKind.TOOL_RESULT
        and "output" in item.payload
        and "error_class" not in item.payload
    }
    return mutating & succeeded


# 从工具调用和结果中提取实际发生变更的工作区路径
def _changed_files(items: Iterable[TurnItemRecord]) -> list[str]:
    records = list(items)
    changed: set[str] = set()
    mutating_call_ids = _successful_mutating_call_ids(records)
    for item in records:
        if item.kind not in {TurnItemKind.TOOL_CALL, TurnItemKind.TOOL_RESULT}:
            continue
        if item.tool_call_id not in mutating_call_ids:
            continue
        params = item.payload.get("params")
        if isinstance(params, dict):
            path = params.get("path")
            if isinstance(path, str) and path:
                changed.add(path.replace("\\", "/"))
        paths = item.payload.get("paths")
        if isinstance(paths, list):
            changed.update(
                path.replace("\\", "/")
                for path in paths
                if isinstance(path, str) and path
            )
        output = _tool_output(item)
        output_path = output.get("path")
        if isinstance(output_path, str) and output_path:
            changed.add(output_path.replace("\\", "/"))
        output_files = output.get("files")
        if isinstance(output_files, list):
            for file_info in output_files:
                if isinstance(file_info, dict) and isinstance(file_info.get("path"), str):
                    changed.add(str(file_info["path"]).replace("\\", "/"))
    return sorted(changed)


# 将成功文件工具结果聚合为逐路径增删行，缺少任一操作统计时保留未知而不猜测
def _file_changes(
    items: Iterable[TurnItemRecord],
    files_changed: list[str],
) -> list[TurnFileChangeReceipt]:
    records = list(items)
    successful_ids = _successful_mutating_call_ids(records)
    calls = {
        item.tool_call_id: item
        for item in records
        if item.kind == TurnItemKind.TOOL_CALL and item.tool_call_id in successful_ids
    }
    operations: dict[str, list[tuple[int | None, int | None]]] = {
        path: [] for path in files_changed
    }
    for item in records:
        if item.kind != TurnItemKind.TOOL_RESULT or item.tool_call_id not in successful_ids:
            continue
        output = _tool_output(item)
        raw_files = output.get("files")
        if isinstance(raw_files, list):
            for raw in raw_files:
                if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                    continue
                path = str(raw["path"]).replace("\\", "/")
                additions = raw.get("additions")
                deletions = raw.get("deletions", raw.get("removals"))
                operations.setdefault(path, []).append(
                    (
                        additions if isinstance(additions, int) else None,
                        deletions if isinstance(deletions, int) else None,
                    )
                )
            continue
        call = calls.get(item.tool_call_id)
        params = call.payload.get("params") if call is not None else None
        raw_path = output.get("path")
        if not isinstance(raw_path, str) and isinstance(params, dict):
            raw_path = params.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            continue
        path = raw_path.replace("\\", "/")
        additions = output.get("additions")
        deletions = output.get("deletions", output.get("removals"))
        operations.setdefault(path, []).append(
            (
                additions if isinstance(additions, int) else None,
                deletions if isinstance(deletions, int) else None,
            )
        )

    changes: list[TurnFileChangeReceipt] = []
    for path in sorted(operations):
        stats = operations[path]
        known = bool(stats) and all(
            additions is not None and deletions is not None
            for additions, deletions in stats
        )
        changes.append(
            TurnFileChangeReceipt(
                path=path,
                additions=(sum(item[0] or 0 for item in stats) if known else None),
                deletions=(sum(item[1] or 0 for item in stats) if known else None),
            )
        )
    return changes


# 将 tool result 的 JSON output 解码为结构化事实
def _tool_output(item: TurnItemRecord) -> dict[str, Any]:
    if item.kind != TurnItemKind.TOOL_RESULT:
        return {}
    output = item.payload.get("output")
    if not isinstance(output, str):
        return {}
    try:
        decoded = json.loads(output)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


# 从显式 artifact item 和工具 spill 输出提取 artifact 引用
def _artifacts(items: Iterable[TurnItemRecord]) -> list[dict[str, JsonValue]]:
    artifacts: list[dict[str, JsonValue]] = []
    for item in items:
        if item.kind == TurnItemKind.ARTIFACT:
            artifacts.append(dict(item.payload))
            continue
        artifact = _tool_output(item).get("artifact")
        if isinstance(artifact, dict):
            artifacts.append(_JSON_OBJECT.validate_python(artifact))
    return artifacts


# 从工具 mutation result 提取 checkpoint 元数据
def _checkpoints(items: Iterable[TurnItemRecord]) -> list[dict[str, JsonValue]]:
    checkpoints: list[dict[str, JsonValue]] = []
    for item in items:
        output = _tool_output(item)
        checkpoint_id = output.get("checkpoint_id")
        if isinstance(checkpoint_id, str) and checkpoint_id:
            checkpoints.append({"checkpoint_id": checkpoint_id})
    return checkpoints


# 汇总工具、后台任务和 hook 事件中的受管进程资源证据
def _process_usage(events: Iterable[RuntimeEventRecord]) -> TurnProcessUsageReceipt:
    records: list[dict[str, JsonValue]] = []
    for event in events:
        raw = event.payload.get("process_usage")
        if isinstance(raw, dict) and raw:
            records.append(raw)
    return TurnProcessUsageReceipt(
        record_count=len(records),
        complete_records=sum(bool(record.get("complete")) for record in records),
        total_process_wall_ms=sum(_json_count(record.get("wall_time_ms")) for record in records),
        user_cpu_ms=sum(_json_count(record.get("user_cpu_ms")) for record in records),
        system_cpu_ms=sum(_json_count(record.get("system_cpu_ms")) for record in records),
        peak_memory_bytes=max(
            (_json_count(record.get("peak_memory_bytes")) for record in records),
            default=0,
        ),
        process_count=sum(_json_count(record.get("process_count")) for record in records),
    )


# 从持久化 turn、item 和 event 纯函数构建可离线读取的收据
def build_turn_receipt(
    turn: TurnRecord,
    items: list[TurnItemRecord],
    events: list[RuntimeEventRecord],
    *,
    workspace: str = "",
) -> TurnReceipt:
    event_types = [event.type for event in events]
    requested = event_types.count("permission.requested")
    granted = event_types.count("permission.granted")
    denied = event_types.count("permission.denied")
    checkpoints = _checkpoints(items)
    checkpoints.extend(_unique_payloads(events, {"checkpoint.created"}, key="checkpoint_id"))
    artifacts = _artifacts(items)
    artifacts.extend(_unique_payloads(events, {"task.artifact_added"}, key="artifact"))
    workers = _unique_payloads(
        events,
        {"subagent.started", "subagent.finished", "worker.started", "worker.finished"},
        key="worker_run_id",
    )
    verification = _unique_payloads(
        events,
        {"lsp.diagnostics", "verification.completed", "verification.failed"},
        key="tool",
    )
    context_selection = _unique_payloads(
        events,
        {"context.repository"},
        key="repository_hash",
    )
    finished_payload = next(
        (dict(event.payload) for event in reversed(events) if event.type == "run.finished"),
        {},
    )
    raw_outcome = finished_payload.get("outcome")
    outcome = (
        cast(RunOutcome, raw_outcome)
        if isinstance(raw_outcome, str) and raw_outcome in _RUN_OUTCOMES
        else None
    )
    raw_failure_category = finished_payload.get("failure_category")
    failure_category = (
        str(raw_failure_category)
        if isinstance(raw_failure_category, str) and raw_failure_category
        else None
    )
    raw_result_summary = finished_payload.get("result_summary")
    result_summary = (
        str(raw_result_summary)
        if isinstance(raw_result_summary, str) and raw_result_summary
        else None
    )
    profiled_payload = next(
        (dict(event.payload) for event in events if event.type == "task.profiled"),
        {},
    )
    raw_task_profile = profiled_payload.get("profile")
    task_profile = dict(raw_task_profile) if isinstance(raw_task_profile, dict) else {}
    raw_profile_digest = profiled_payload.get("profile_digest")
    task_profile_digest = (
        str(raw_profile_digest) if isinstance(raw_profile_digest, str) else ""
    )
    files_changed = _changed_files(items)
    changes = _file_changes(items, files_changed)
    unavailable: list[str] = []
    if turn.route is None:
        unavailable.append("route")
    if not turn.usage:
        unavailable.append("usage")
    cost = turn.usage.get("estimated_cost_usd", "unknown")
    if cost == "unknown":
        unavailable.append("cost")
    for name, value in (
        ("files_changed", files_changed),
        ("changes", changes),
        ("checkpoints", checkpoints),
        ("artifacts", artifacts),
        ("workers", workers),
        ("verification", verification),
        ("context_selection", context_selection),
    ):
        if not value:
            unavailable.append(name)
    if any(change.additions is None or change.deletions is None for change in changes):
        unavailable.append("change_line_stats")
    error_classification = None
    if turn.error is not None:
        raw_error = turn.error.get("classification") or turn.error.get("reason")
        error_classification = str(raw_error) if raw_error is not None else None
    if error_classification is None:
        error_classification = failure_category
    if error_classification is None and turn.status in {TurnStatus.FAILED, TurnStatus.INTERRUPTED}:
        unavailable.append("error_classification")
    requested_tier = (
        tier_for_auto_review(turn.sandbox)
        if turn.authority_profile == AuthorityProfile.AUTO_REVIEW
        else SandboxTier.NONE
    )
    sandbox_plan = plan_sandbox(
        turn.sandbox,
        requested_tier,
        workspace or ".",
    ).describe()
    if not workspace:
        sandbox_plan["workspace"] = ""
    return TurnReceipt(
        turn_id=turn.id,
        thread_id=turn.thread_id,
        route=turn.route,
        authority=TurnAuthorityReceipt(
            mode=turn.mode,
            profile=turn.authority_profile,
            workspace_trust=turn.workspace_trust,
            sandbox=turn.sandbox,
            sandbox_plan=SandboxPlanReceipt.model_validate(sandbox_plan),
            allowed_actions=turn.allowed_actions,
        ),
        started_at=turn.created_at,
        finished_at=turn.updated_at if turn.status in _TERMINAL else None,
        status=turn.status,
        usage=turn.usage,
        cost=cost,
        tool_call_count=sum(item.kind == TurnItemKind.TOOL_CALL for item in items),
        approvals=TurnApprovalCounts(
            requested=requested,
            granted=granted,
            denied=denied,
        ),
        process_usage=_process_usage(events),
        files_changed=files_changed,
        changes=changes,
        checkpoints=checkpoints,
        artifacts=artifacts,
        workers=workers,
        verification=verification,
        context_selection=context_selection,
        error_classification=error_classification,
        unavailable=unavailable,
        outcome=outcome,
        failure_category=failure_category,
        result_summary=result_summary,
        task_profile=task_profile,
        task_profile_digest=task_profile_digest,
    )

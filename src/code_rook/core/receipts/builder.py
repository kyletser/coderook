from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import JsonValue, TypeAdapter

from code_rook.core.authority import AuthorityProfile
from code_rook.core.receipts.models import (
    SandboxPlanReceipt,
    TurnApprovalCounts,
    TurnAuthorityReceipt,
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
    if tool_name in {"apply_patch", "edit_file", "write_file"}:
        return True
    params = item.payload.get("params")
    if tool_name != "File" or not isinstance(params, dict):
        return False
    action = params.get("action")
    return isinstance(action, str) and action in {"write", "edit", "patch"}


# 从工具调用和结果中提取实际发生变更的工作区路径
def _changed_files(items: Iterable[TurnItemRecord]) -> list[str]:
    records = list(items)
    changed: set[str] = set()
    mutating_call_ids = {
        item.tool_call_id for item in records if _is_mutating_file_call(item)
    }
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
    files_changed = _changed_files(items)
    unavailable: list[Any] = []
    if turn.route is None:
        unavailable.append("route")
    if not turn.usage:
        unavailable.append("usage")
    cost = turn.usage.get("estimated_cost_usd", "unknown")
    if cost == "unknown":
        unavailable.append("cost")
    for name, value in (
        ("files_changed", files_changed),
        ("checkpoints", checkpoints),
        ("artifacts", artifacts),
        ("workers", workers),
        ("verification", verification),
        ("context_selection", context_selection),
    ):
        if not value:
            unavailable.append(name)
    error_classification = None
    if turn.error is not None:
        raw_error = turn.error.get("classification") or turn.error.get("reason")
        error_classification = str(raw_error) if raw_error is not None else None
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
        checkpoints=checkpoints,
        artifacts=artifacts,
        workers=workers,
        verification=verification,
        context_selection=context_selection,
        error_classification=error_classification,
        unavailable=unavailable,
    )

from datetime import UTC, datetime, timedelta

from code_rook.core.authority import (
    AuthorityProfile,
    RuntimeMode,
    SandboxCapability,
    ToolAction,
    WorkspaceTrust,
)
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.receipts.builder import build_turn_receipt
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)


# 返回 receipt 测试使用的稳定 UTC 时间
def _now() -> datetime:
    return datetime(2026, 8, 4, 8, 0, tzinfo=UTC)


# 功能：验证 receipt 从 durable records 完整聚合路线、权限、用量、工具、审批和证据
# 设计：仅传入模型记录而不构造 service/store，证明 builder 是可离线重放的纯函数
def test_receipt_builds_from_durable_records_only() -> None:
    now = _now()
    turn = TurnRecord(
        id="turn-1",
        thread_id="thread-1",
        status=TurnStatus.COMPLETED,
        mode=RuntimeMode.OPERATE,
        authority_profile=AuthorityProfile.FULL_ACCESS,
        workspace_trust=WorkspaceTrust.TRUSTED,
        sandbox=SandboxCapability(available=True, kind="linux_bwrap", reason="isolated"),
        allowed_actions=frozenset({ToolAction.READ, ToolAction.MUTATE}),
        route=RouteReceipt(
            route_id="route-1",
            wire_format="openai_responses",
            base_url_origin="https://api.example.test",
            model="model-1",
            credential_source="keyring",
        ),
        usage={"input_tokens": 40, "output_tokens": 10},
        created_at=now,
        updated_at=now + timedelta(seconds=5),
    )
    items = [
        TurnItemRecord(
            id="call-1",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="tool-1",
            payload={
                "tool_name": "write_file",
                "params": {"path": "src\\main.py"},
            },
            created_at=now + timedelta(seconds=1),
        ),
        TurnItemRecord(
            id="result-1",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="tool-1",
            payload={
                "tool_name": "write_file",
                "status": "ok",
                "output": (
                    '{"checkpoint_id":"cp-output","artifact":'
                    '{"uri":"artifact://tool-output"}}'
                ),
            },
            created_at=now + timedelta(seconds=2),
        ),
        TurnItemRecord(
            id="artifact-1",
            turn_id=turn.id,
            kind=TurnItemKind.ARTIFACT,
            payload={"uri": "artifact://report"},
            created_at=now + timedelta(seconds=3),
        ),
    ]
    events = [
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=1,
            type="permission.requested",
            payload={"tool_use_id": "tool-1"},
            ts=now,
        ),
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=2,
            type="permission.granted",
            payload={"tool_use_id": "tool-1"},
            ts=now,
        ),
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=3,
            type="checkpoint.created",
            payload={"checkpoint_id": "cp-1"},
            ts=now,
        ),
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=4,
            type="subagent.finished",
            payload={"run_id": "worker-1", "status": "success"},
            ts=now,
        ),
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=5,
            type="lsp.diagnostics",
            payload={"tool": "ruff", "status": "ok"},
            ts=now,
        ),
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=7,
            type="context.repository",
            payload={
                "repository_hash": "repo-1",
                "paths": ["src/main.py"],
                "selection_reasons": [
                    {"path": "src/main.py", "reasons": ["query_path:main"]}
                ],
            },
            ts=now,
        ),
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=6,
            type="tool.call_finished",
            payload={
                "process_usage": {
                    "wall_time_ms": 120,
                    "user_cpu_ms": 30,
                    "system_cpu_ms": 10,
                    "peak_memory_bytes": 4096,
                    "process_count": 2,
                    "samples": 3,
                    "complete": True,
                }
            },
            ts=now,
        ),
    ]

    receipt = build_turn_receipt(turn, items, events)

    assert receipt.route == turn.route
    assert receipt.authority.mode == RuntimeMode.OPERATE
    assert receipt.finished_at == turn.updated_at
    assert receipt.tool_call_count == 1
    assert receipt.approvals.model_dump() == {"requested": 1, "granted": 1, "denied": 0}
    assert receipt.files_changed == ["src/main.py"]
    assert receipt.changes[0].model_dump() == {
        "path": "src/main.py",
        "additions": None,
        "deletions": None,
    }
    assert {item["checkpoint_id"] for item in receipt.checkpoints} == {"cp-1", "cp-output"}
    assert {item["uri"] for item in receipt.artifacts} == {
        "artifact://report",
        "artifact://tool-output",
    }
    assert receipt.workers[0]["run_id"] == "worker-1"
    assert receipt.verification[0]["status"] == "ok"
    assert receipt.context_selection[0]["repository_hash"] == "repo-1"
    assert receipt.cost == "unknown"
    assert receipt.process_usage.model_dump() == {
        "record_count": 1,
        "complete_records": 1,
        "total_process_wall_ms": 120,
        "user_cpu_ms": 30,
        "system_cpu_ms": 10,
        "peak_memory_bytes": 4096,
        "process_count": 2,
    }
    assert receipt.unavailable == ["cost", "change_line_stats"]


# 功能：Receipt 从默认 File action-family 准确识别写入路径且忽略只读调用
# 设计：同时传入 File.write 和 File.read 配对 item，避免只看 family 名导致读取路径被误报为变更
def test_receipt_tracks_mutating_file_family_actions_only() -> None:
    turn = TurnRecord(
        id="turn-family",
        thread_id="thread-family",
        status=TurnStatus.COMPLETED,
        created_at=_now(),
        updated_at=_now(),
    )
    items = [
        TurnItemRecord(
            id="call-write",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="write-1",
            payload={
                "tool_name": "File",
                "params": {"action": "write", "path": "src\\changed.py"},
            },
            created_at=_now(),
        ),
        TurnItemRecord(
            id="result-write",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="write-1",
            payload={"tool_name": "File", "output": '{"path":"src/changed.py"}'},
            created_at=_now(),
        ),
        TurnItemRecord(
            id="call-read",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="read-1",
            payload={
                "tool_name": "File",
                "params": {"action": "read", "path": "src/unchanged.py"},
            },
            created_at=_now(),
        ),
        TurnItemRecord(
            id="result-read",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="read-1",
            payload={"tool_name": "File", "output": '{"path":"src/unchanged.py"}'},
            created_at=_now(),
        ),
    ]

    receipt = build_turn_receipt(turn, items, [])

    assert receipt.files_changed == ["src/changed.py"]
    assert receipt.changes[0].path == "src/changed.py"


# 功能：Receipt 只把成功文件工具的结构化结果计入逐文件增删行证据
# 设计：串联 write 与 patch 两种结果格式并聚合同一路径，覆盖 deletions/removals 字段兼容
def test_receipt_aggregates_successful_change_line_stats() -> None:
    turn = TurnRecord(
        id="turn-stats",
        thread_id="thread-stats",
        status=TurnStatus.COMPLETED,
        created_at=_now(),
        updated_at=_now(),
    )
    items = [
        TurnItemRecord(
            id="call-write",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="write-1",
            payload={"tool_name": "write_file", "params": {"path": "src/main.py"}},
            created_at=_now(),
        ),
        TurnItemRecord(
            id="result-write",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="write-1",
            payload={
                "tool_name": "write_file",
                "output": (
                    '{"path":"src/main.py","additions":3,"deletions":1}'
                ),
            },
            created_at=_now(),
        ),
        TurnItemRecord(
            id="call-patch",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="patch-1",
            payload={"tool_name": "apply_patch", "params": {"patch": "..."}},
            created_at=_now(),
        ),
        TurnItemRecord(
            id="result-patch",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="patch-1",
            payload={
                "tool_name": "apply_patch",
                "output": (
                    '{"files":[{"path":"src/main.py","additions":2,'
                    '"removals":4}]}'
                ),
            },
            created_at=_now(),
        ),
    ]

    receipt = build_turn_receipt(turn, items, [])

    assert receipt.files_changed == ["src/main.py"]
    assert receipt.changes[0].model_dump() == {
        "path": "src/main.py",
        "additions": 5,
        "deletions": 5,
    }
    assert "change_line_stats" not in receipt.unavailable


# 功能：失败写调用和 apply_patch dry-run 不得伪装成已发生的工作区修改
# 设计：失败结果使用真实 error_class 形态，dry-run 即使返回 output 也必须被调用参数排除
def test_receipt_excludes_failed_and_dry_run_mutations() -> None:
    turn = TurnRecord(
        id="turn-no-change",
        thread_id="thread-no-change",
        status=TurnStatus.FAILED,
        created_at=_now(),
        updated_at=_now(),
    )
    items = [
        TurnItemRecord(
            id="call-failed",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="failed-1",
            payload={"tool_name": "write_file", "params": {"path": "bad.py"}},
            created_at=_now(),
        ),
        TurnItemRecord(
            id="result-failed",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="failed-1",
            payload={"tool_name": "write_file", "error_class": "runtime_error"},
            created_at=_now(),
        ),
        TurnItemRecord(
            id="call-dry",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_CALL,
            tool_call_id="dry-1",
            payload={
                "tool_name": "apply_patch",
                "params": {"patch": "...", "dry_run": True},
            },
            created_at=_now(),
        ),
        TurnItemRecord(
            id="result-dry",
            turn_id=turn.id,
            kind=TurnItemKind.TOOL_RESULT,
            tool_call_id="dry-1",
            payload={
                "tool_name": "apply_patch",
                "output": (
                    '{"files":[{"path":"preview.py","additions":1,'
                    '"removals":0}],"dry_run":true}'
                ),
            },
            created_at=_now(),
        ),
    ]

    receipt = build_turn_receipt(turn, items, [])

    assert receipt.files_changed == []
    assert receipt.changes == []


# 功能：验证缺少可选事实时 receipt 明确报告 unavailable 而不伪造空值含义
# 设计：使用运行中的最小 turn 和空 ledger，逐项断言未知字段并保留未完成时间为空
def test_receipt_marks_unavailable_facts_explicitly() -> None:
    turn = TurnRecord(
        id="turn-minimal",
        thread_id="thread-minimal",
        status=TurnStatus.RUNNING,
        created_at=_now(),
        updated_at=_now(),
    )

    receipt = build_turn_receipt(turn, [], [])

    assert receipt.finished_at is None
    assert set(receipt.unavailable) == {
        "route",
        "usage",
        "cost",
        "files_changed",
        "changes",
        "checkpoints",
        "artifacts",
        "workers",
        "verification",
        "context_selection",
    }


# 功能：验证 TurnReceipt 从 durable run.finished 保留结构化终止语义和安全失败分类
# 设计：让 Turn 状态只能表达 failed，而事件提供 incomplete，断言离线收据仍可恢复精确原因
def test_receipt_preserves_structured_run_outcome() -> None:
    now = _now()
    turn = TurnRecord(
        id="turn-incomplete",
        thread_id="thread-incomplete",
        status=TurnStatus.FAILED,
        created_at=now,
        updated_at=now,
    )
    events = [
        RuntimeEventRecord(
            thread_id=turn.thread_id,
            turn_id=turn.id,
            seq=1,
            type="run.finished",
            payload={
                "status": "failed",
                "outcome": "incomplete",
                "failure_category": "model",
                "result_summary": "generation stopped before completion",
            },
            ts=now,
        )
    ]

    receipt = build_turn_receipt(turn, [], events)

    assert receipt.outcome == "incomplete"
    assert receipt.failure_category == "model"
    assert receipt.error_classification == "model"
    assert receipt.result_summary == "generation stopped before completion"

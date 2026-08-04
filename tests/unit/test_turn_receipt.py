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
    ]

    receipt = build_turn_receipt(turn, items, events)

    assert receipt.route == turn.route
    assert receipt.authority.mode == RuntimeMode.OPERATE
    assert receipt.finished_at == turn.updated_at
    assert receipt.tool_call_count == 1
    assert receipt.approvals.model_dump() == {"requested": 1, "granted": 1, "denied": 0}
    assert receipt.files_changed == ["src/main.py"]
    assert {item["checkpoint_id"] for item in receipt.checkpoints} == {"cp-1", "cp-output"}
    assert {item["uri"] for item in receipt.artifacts} == {
        "artifact://report",
        "artifact://tool-output",
    }
    assert receipt.workers[0]["run_id"] == "worker-1"
    assert receipt.verification[0]["status"] == "ok"
    assert receipt.cost == "unknown"
    assert receipt.unavailable == ["cost"]


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
        "checkpoints",
        "artifacts",
        "workers",
        "verification",
    }

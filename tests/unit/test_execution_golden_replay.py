from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from code_rook.core.bus.events import (
    PermissionDeniedEvent,
    PermissionRequestedEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.execution.ledger import SessionLedgerBridge
from code_rook.core.session.store import SessionStore, SessionTranscriptSink

_STAMP = "2026-08-24T00:00:00Z"
_GOLDEN = Path(__file__).parents[1] / "fixtures" / "golden" / "execution_replay_v2.json"


# 把持久事件压缩为包含顺序和关键语义的稳定 Golden 轨迹
def _normalize_trace(store: SessionStore, session_id: str) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    payload_keys = (
        "role",
        "content",
        "message_id",
        "goal",
        "tool_use_id",
        "tool_name",
        "decision",
        "error_class",
        "status",
        "outcome",
        "result_summary",
    )
    for event in store.read_session_events(session_id):
        item: dict[str, Any] = {"seq": event.seq, "type": event.type}
        if event.turn_id:
            item["turn_id"] = event.turn_id
        if event.step_id:
            item["step_id"] = event.step_id
        for key in payload_keys:
            value = event.payload.get(key)
            if value not in (None, ""):
                item[key] = value
        block = event.payload.get("block")
        if isinstance(block, dict):
            item["block"] = {
                key: block[key]
                for key in ("type", "text", "id", "name", "tool_use_id", "content", "is_error")
                if key in block
            }
        trace.append(item)
    return trace


# 通过真实 EventBus、Ledger Bridge 和 Transcript Sink 记录一个固定场景
async def _record_scenario(root: Path, scenario: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    session_id = f"sess-{scenario.replace('_', '-')}"
    run_id = f"run-{scenario.split('_')[0] if scenario != 'permission_denied' else 'denied'}"
    goal = {"text_response": "hello", "tool_success": "read", "permission_denied": "delete"}[scenario]
    store = SessionStore(root)
    store.append_message(session_id, "user", goal, run_id=run_id, message_id=f"{run_id}:user")
    bus = EventBus()
    bridge = SessionLedgerBridge(store, session_id, run_id=run_id)
    bridge.subscribe(bus)
    transcript = SessionTranscriptSink(store, session_id, run_id)

    await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_STAMP))
    await bus.publish(StepStartedEvent(run_id=run_id, step=1, ts=_STAMP))
    if scenario == "text_response":
        transcript.append_assistant(1, [{"type": "text", "text": "done"}])
        result_summary = "done"
        status = "success"
    else:
        tool_id = "tool-1" if scenario == "tool_success" else "tool-denied"
        tool_name = "read_file" if scenario == "tool_success" else "bash"
        tool_input = {"path": "a.py"} if scenario == "tool_success" else {"command": "rm file"}
        transcript.append_assistant(
            1,
            [{"type": "tool_use", "id": tool_id, "name": tool_name, "input": tool_input}],
        )
        await bus.publish(
            ToolCallStartedEvent(
                run_id=run_id,
                tool_use_id=tool_id,
                tool_name=tool_name,
                params=tool_input,
                step=1,
                ts=_STAMP,
            )
        )
        if scenario == "tool_success":
            await bus.publish(
                ToolCallFinishedEvent(
                    run_id=run_id,
                    tool_use_id=tool_id,
                    tool_name=tool_name,
                    elapsed_ms=1,
                    output="contents",
                    step=1,
                    ts=_STAMP,
                )
            )
            transcript.append_tool_result(
                1,
                tool_id,
                "contents",
                is_error=False,
                block_index=0,
                block_count=1,
            )
            result_summary = "read complete"
            status = "success"
        else:
            await bus.publish(
                PermissionRequestedEvent(
                    run_id=run_id,
                    tool_use_id=tool_id,
                    tool_name=tool_name,
                    params=tool_input,
                    param_preview="rm file",
                    session_id=session_id,
                    ts=_STAMP,
                )
            )
            await bus.publish(
                PermissionDeniedEvent(
                    run_id=run_id,
                    tool_use_id=tool_id,
                    decision="deny_once",
                    ts=_STAMP,
                )
            )
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_id,
                    tool_name=tool_name,
                    error_class="permission_denied",
                    error_message="denied",
                    elapsed_ms=1,
                    step=1,
                    ts=_STAMP,
                )
            )
            transcript.append_tool_result(
                1,
                tool_id,
                "denied",
                is_error=True,
                block_index=0,
                block_count=1,
            )
            result_summary = None
            status = "failed"

    await bus.publish(StepFinishedEvent(run_id=run_id, step=1, ts=_STAMP))
    await bus.publish(
        RunFinishedEvent(
            run_id=run_id,
            status=status,
            steps=1,
            outcome="completed" if status == "success" else "failed",
            result_summary=result_summary,
            ts=_STAMP,
        )
    )
    await bridge.close()

    replay = SessionStore(root)
    assert replay.verify_ledger(session_id) == []
    assert replay.verify_execution_ledger(session_id) == []
    return _normalize_trace(replay, session_id), replay.read_messages(session_id)


# 功能：验证纯文本 Turn 在 daemon 风格重开后仍与固定 Golden 轨迹一致
# 设计：经过真实总线和文件账本记录后新建 SessionStore 回放，覆盖最小成功路径
async def test_text_response_golden_replay(tmp_path: Path) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))["text_response"]
    trace, messages = await _record_scenario(tmp_path, "text_response")
    assert trace == golden["trace"]
    assert messages == golden["messages"]


# 功能：验证 Tool Call、Result 和模型消息在重开后保持固定顺序与配对
# 设计：记录完整工具闭环并同时比较事件 Golden 与 derive_messages 投影
async def test_tool_success_golden_replay(tmp_path: Path) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))["tool_success"]
    trace, messages = await _record_scenario(tmp_path, "tool_success")
    assert trace == golden["trace"]
    assert messages == golden["messages"]


# 功能：验证权限拒绝不会在重放时变成工具成功或丢失失败证据
# 设计：固定 requested、resolved、failed 和错误 tool_result 全链，覆盖安全终态语义
async def test_permission_denied_golden_replay(tmp_path: Path) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))["permission_denied"]
    trace, messages = await _record_scenario(tmp_path, "permission_denied")
    assert trace == golden["trace"]
    assert messages == golden["messages"]

from __future__ import annotations

import json
from pathlib import Path

from code_rook.core.events.bus import EventBus
from code_rook.core.session.exporter import export_session
from code_rook.core.session.importer import import_session_content
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore


# 功能：验证 CodeRook JSON 导出可以导回原生 v2 Ledger 并继续使用。
# 设计：先走真实 exporter，再由 SessionManager 导入并比较消息、笔记与审计事件。
async def test_coderook_export_import_roundtrip(tmp_path: Path) -> None:
    source = Session(
        id="sess-source",
        mode="chat",
        status="waiting_for_input",
        title="Portable task",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
    )
    expected = [
        {"role": "user", "content": "inspect this"},
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
    ]
    _, _, exported = export_session(source, expected, "keep this note\n", "json")
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: None, EventBus())  # type: ignore[arg-type]

    imported, count, source_format = await manager.import_session(
        exported,
        filename="portable.json",
    )

    assert imported.title == "Portable task"
    assert imported.status == "waiting_for_input"
    assert count == 2
    assert source_format == "coderook-json"
    assert store.read_messages(imported.id) == expected
    assert store.read_notes(imported.id) == "keep this note\n"
    assert manager.context_info(imported.id)["navigation"] == {
        "thread_id": imported.id,
        "ledger_seq": 3,
        "messages": expected,
        "excluded_turn_ids": [],
    }
    audit = store.read_session_events(imported.id)[-1]
    assert audit.type == "session.imported"
    assert audit.payload == {
        "source_format": "coderook-json",
        "filename": "portable.json",
        "message_count": 2,
    }


# 功能：验证 Pi JSONL 只导入最后活动分支并转换工具调用协议。
# 设计：构造一条主分支和一条废弃分支，断言 parentId 回溯与 toolResult 映射。
def test_pi_jsonl_imports_active_branch_and_tool_messages() -> None:
    rows = [
        {"type": "session", "version": 3, "id": "pi-1", "timestamp": "now", "cwd": "."},
        {
            "type": "message",
            "id": "u1",
            "parentId": None,
            "timestamp": "now",
            "message": {"role": "user", "content": "read it", "timestamp": 1},
        },
        {
            "type": "message",
            "id": "abandoned",
            "parentId": "u1",
            "timestamp": "now",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "old"}]},
        },
        {
            "type": "message",
            "id": "a1",
            "parentId": "u1",
            "timestamp": "now",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "a.py"}}
                ],
            },
        },
        {
            "type": "message",
            "id": "r1",
            "parentId": "a1",
            "timestamp": "now",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "toolName": "read",
                "content": [{"type": "text", "text": "contents"}],
                "isError": False,
            },
        },
        {"type": "session_info", "id": "name", "parentId": "r1", "timestamp": "now", "name": "Pi task"},
    ]

    imported = import_session_content("\n".join(json.dumps(row) for row in rows))

    assert imported.source_format == "pi-jsonl"
    assert imported.title == "Pi task"
    assert imported.messages == [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a.py"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "contents"}],
        },
    ]

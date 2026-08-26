from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from code_rook.core.compact.compactor import Compactor
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.session.store import SessionStore


# 构造包含重复大工具结果且保持调用闭环的长消息历史
def _messages() -> list[dict[str, Any]]:
    repeated = "same file content\n" * 80
    messages: list[dict[str, Any]] = []
    for index in range(5):
        messages.extend(
            [
                {"role": "user", "content": f"inspect round {index} " + "x" * 300},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"read-{index}",
                            "name": "File",
                            "input": {"action": "read", "path": "src/app.py"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": f"read-{index}",
                            "content": repeated,
                        }
                    ],
                },
            ]
        )
    return messages


# 根据 Ledger 事实构造逐字保留 ID、正文和来源序号的摘要响应
def _provider_with_facts(store: SessionStore, session_id: str) -> Any:
    events = store.read_session_events(session_id)
    goal_event = next(event for event in events if event.type == "input.admitted")
    profile_event = next(event for event in events if event.type == "task.profiled")
    facts = [
        {
            "id": "current_goal",
            "text": json.dumps(
                goal_event.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:2_000],
            "source_event_seqs": [goal_event.seq],
        },
        {
            "id": "task_profile",
            "text": json.dumps(
                profile_event.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )[:2_000],
            "source_event_seqs": [profile_event.seq],
        },
    ]
    payload = {
        "goal": "keep the task reliable",
        "completed": [],
        "constraints": [],
        "decisions": [],
        "files": [{"path": "src/app.py", "state": "inspected"}],
        "todos": ["finish verification"],
        "errors": [],
        "critical_data": [],
        "pinned_facts": facts,
    }
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            text=json.dumps(payload),
            usage=UsageStats(input_tokens=100, output_tokens=50),
        )
    )
    return provider


# 功能：验证证据压缩完整保留 Ledger 固定事实并折叠重复读取
# 设计：写入真实 v2 事件后用忠实摘要 stub，检查来源覆盖率和去重计数
async def test_adaptive_compaction_preserves_pinned_facts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session_id = "sess-adaptive"
    store.append_session_event(
        session_id,
        event_type="input.admitted",
        turn_id="run-1",
        payload={"role": "user", "content": "keep compatibility"},
    )
    store.append_session_event(
        session_id,
        event_type="task.profiled",
        turn_id="run-1",
        payload={"profile": {"strategy": "plan_first"}},
    )
    compactor = Compactor(
        EventBus(),
        store.session_dir(session_id),
        session_id,
        store=store,
        retain_ratio=0.2,
        strategy="adaptive_evidence",
    )

    result = await compactor.compact_messages(
        _messages(),
        _provider_with_facts(store, session_id),
    )

    assert result is not None
    assert result.pinned_fact_count == result.pinned_fact_retained == 2
    assert result.deduplicated_reads >= 1


# 功能：验证摘要遗漏任一来源事实时原上下文不会被替换
# 设计：对同一真实 Ledger 返回旧版无 pinned_facts 摘要，锁定事实覆盖失败回退
async def test_adaptive_compaction_rejects_missing_pinned_fact(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session_id = "sess-missing"
    store.append_session_event(
        session_id,
        event_type="input.admitted",
        turn_id="run-1",
        payload={"role": "user", "content": "must keep this"},
    )
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            text=json.dumps({"goal": "task"}),
        )
    )

    result = await Compactor(
        EventBus(),
        store.session_dir(session_id),
        session_id,
        store=store,
        strategy="adaptive_evidence",
    ).compact_messages(_messages(), provider)

    assert result is None

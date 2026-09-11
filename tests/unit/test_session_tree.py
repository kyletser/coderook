from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_rook.core.session.store import SessionStore


# 功能：导航展示投影固定在切换点，不把后续 Turn 重复包含进历史。
# 设计：切换后写入新回答并重开存储，验证历史起点与排除的旧 run 集合稳定。
def test_navigation_projection_stays_fixed_after_new_turn(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-nav", "user", "Question", run_id="old-run")
    store.append_message("sess-nav", "assistant", "Answer", run_id="old-run")
    target = store.read_session_events("sess-nav")[-1].seq
    assert store.navigation_projection("sess-nav") is None
    store.navigate_tree("sess-nav", target)
    store.append_message("sess-nav", "user", "Continue", run_id="new-run")
    projection = SessionStore(tmp_path).navigation_projection("sess-nav")
    assert projection is not None
    assert projection["excluded_turn_ids"] == ["old-run"]
    assert [message["content"] for message in projection["messages"]] == ["Question", "Answer"]


# 功能：分支历史投影保留直接 Shell 的展示角色，不把执行记录伪装成用户模型消息。
# 设计：追加真实 shell 完成事件后选择该节点，重开存储并核对展示投影的结构化字段。
def test_navigation_projection_preserves_shell_display(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    event = store.append_session_event(
        "sess-shell",
        event_type="user.shell_completed",
        turn_id="run-shell",
        payload={
            "command": "echo visible",
            "output": "visible",
            "status": "success",
            "exclude_from_context": False,
        },
    )
    store.select_branch("sess-shell", event.seq)

    projection = SessionStore(tmp_path).navigation_projection("sess-shell")

    assert projection is not None
    assert projection["messages"] == [{
        "role": "bashExecution",
        "command": "echo visible",
        "output": "visible",
        "status": "success",
        "exclude_from_context": False,
        "run_id": "run-shell",
    }]


# 功能：选中用户消息回填输入框，根节点导航不删除其他历史分支。
# 设计：使用真实追加日志切到空根并重新回答，再重开原路径校验完整消息。
def test_navigate_user_message_and_restore_branch(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-nav", "user", "Original question")
    question = store.read_session_events("sess-nav")[-1].seq
    store.append_message("sess-nav", "assistant", "Original answer")
    answer = store.read_session_events("sess-nav")[-1].seq
    before = (store.session_dir("sess-nav") / "thread.jsonl").read_bytes()
    result = store.navigate_tree("sess-nav", question)
    assert result["editor_text"] == "Original question"
    assert result["leaf_seq"] == 0
    assert store.read_messages("sess-nav") == []
    store.append_message("sess-nav", "user", "Alternative question")
    assert (store.session_dir("sess-nav") / "thread.jsonl").read_bytes().startswith(before)
    reopened = SessionStore(tmp_path)
    result = reopened.navigate_tree("sess-nav", answer)
    assert result["editor_text"] == ""
    assert [m["content"] for m in reopened.read_messages("sess-nav")] == [
        "Original question", "Original answer",
    ]


# 功能：会话管理器持久通知路径切换，不创建新会话且活动 Turn 中禁止导航。
# 设计：真实 Runtime 和文件 Ledger 联动，检查事件关联序号与异步锁行为。
async def test_manager_navigation_is_durable(tmp_path: Path) -> None:
    from code_rook.core.bus.envelope import HandlerError
    from code_rook.core.events.bus import EventBus
    from code_rook.core.runtime.service import RuntimeService
    from code_rook.core.runtime.store import RuntimeStore
    from code_rook.core.session.manager import SessionManager

    bus = EventBus()
    store = SessionStore(tmp_path / "sessions")
    runtime = RuntimeService(RuntimeStore(tmp_path / "runtime.db"), tmp_path, bus=bus)
    manager = SessionManager(store, MagicMock(), bus, runtime_service=runtime)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "Revise this")
    target = store.read_session_events(session.id)[-1].seq
    result = await manager.navigate_tree(session.id, target)
    assert result["session_id"] == session.id
    events = await runtime.list_events(session.id)
    assert events[-1].type == "session.navigated"
    assert events[-1].payload["ledger_seq"] == result["ledger_seq"]
    async with manager._locks[session.id]:
        with pytest.raises(HandlerError, match="active turn"):
            await manager.navigate_tree(session.id, target)


# 功能：分支选择只改变当前上下文，原分支保持可恢复且重开进程后不串线。
# 设计：建立两条具有相同前缀的分支并来回选中，用真实日志验证追加和父指针投影。
def test_branch_switch_preserves_all_history(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "Original question")
    root = store.read_session_events("sess-1")[-1].seq
    store.append_message("sess-1", "assistant", "First answer")
    first = store.read_session_events("sess-1")[-1].seq
    path = store.session_dir("sess-1") / "thread.jsonl"
    before = path.read_bytes()
    store.select_branch("sess-1", root)
    store.append_message("sess-1", "assistant", "Alternative answer")
    alternative = store.read_session_events("sess-1")[-1].seq
    assert path.read_bytes().startswith(before)
    assert [message["content"] for message in store.derive_messages("sess-1")] == [
        "Original question",
        "Alternative answer",
    ]
    store.select_branch("sess-1", first)
    reopened = SessionStore(tmp_path)
    assert [message["content"] for message in reopened.derive_messages("sess-1")] == [
        "Original question",
        "First answer",
    ]
    reopened.select_branch("sess-1", alternative)
    assert reopened.derive_messages("sess-1")[-1]["content"] == "Alternative answer"
    assert any(not entry["active"] for entry in reopened.session_tree("sess-1"))


# 功能：v2 Ledger 的文本回答在创建替代分支后仍可从历史面板恢复。
# 设计：写入真实 input/llm 事件并切到根创建新回答，再通过旧回答预览节点恢复原路径。
def test_session_tree_exposes_and_restores_v2_assistant_answer(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_session_event(
        "sess-v2",
        event_type="input.admitted",
        payload={"role": "user", "content": "Original question"},
    )
    question = store.read_session_events("sess-v2")[-1].seq
    store.append_session_event(
        "sess-v2",
        event_type="llm.message",
        payload={
            "role": "assistant",
            "block": {"type": "text", "text": "Original answer"},
            "message_id": "answer",
            "block_id": "answer:0",
            "block_index": 0,
            "block_count": 1,
        },
    )
    answer = store.read_session_events("sess-v2")[-1].seq
    store.navigate_tree("sess-v2", question)
    store.append_session_event(
        "sess-v2",
        event_type="input.admitted",
        payload={"role": "user", "content": "Alternative question"},
    )

    answer_entry = next(
        entry for entry in store.session_tree("sess-v2") if entry["seq"] == answer
    )
    store.navigate_tree("sess-v2", answer)

    assert answer_entry["preview"] == "Original answer"
    assert answer_entry["active"] is False
    assert store.derive_messages("sess-v2") == [
        {"role": "user", "content": "Original question"},
        {"role": "assistant", "content": [{"type": "text", "text": "Original answer"}]},
    ]


# 功能：某分支的压缩只在该分支生效，不遮蔽其他分支的原始消息。
# 设计：在第一分支提交摘要后回到共享前缀创建第二分支，再恢复第一分支摘要。
def test_compaction_is_scoped_to_selected_branch(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "Goal")
    root = store.read_session_events("sess-1")[-1].seq
    store.append_message("sess-1", "assistant", "Branch A")
    store.append_compaction("sess-1", [{"role": "user", "content": "Summary A"}], run_id="run-a")
    compacted = store.read_session_events("sess-1")[-1].seq
    store.select_branch("sess-1", root)
    store.append_message("sess-1", "assistant", "Branch B")
    assert [message["content"] for message in store.derive_messages("sess-1")] == [
        "Goal",
        "Branch B",
    ]
    active = store._active_model_ledger_seqs("sess-1")
    assert root in active
    store.select_branch("sess-1", compacted)
    assert store.derive_messages("sess-1") == [{"role": "user", "content": "Summary A"}]


# 功能：未完成的工具调用不可作为继续执行的分支边界。
# 设计：在原始日志保留完整调用但选中缺结果的节点，确认失败不增加分支事件。
def test_branch_rejects_incomplete_tool_call(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message(
        "sess-1", "assistant", [{"type": "tool_use", "id": "t", "name": "read", "input": {}}]
    )
    events = store.read_session_events("sess-1")
    with pytest.raises(ValueError, match="complete tool result"):
        store.select_branch("sess-1", events[-1].seq)
    assert store.read_session_events("sess-1") == events

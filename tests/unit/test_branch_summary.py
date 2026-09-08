from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.agent_runtime.branch_summary import summarize_branch
from code_rook.core.api.service import RuntimeApiService
from code_rook.core.app import CoreApp
from code_rook.core.bus.events import LlmUsageEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.store import SessionStore


# 功能：扩展要求替换摘要指令时不再拼接内建模板，但仍携带待摘要对话。
# 设计：直接捕获 Provider 请求，隔离验证自定义指令与 conversation 数据边界。
async def test_branch_summary_can_replace_default_instructions() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Summary"))
    entries = [(1, {
        "ledger_seq": 1, "type": "input.admitted",
        "payload": {"role": "user", "content": "Historical request"},
    })]
    await summarize_branch(
        entries, provider, focus="Use only compact facts.", replace_instructions=True,
    )
    prompt = provider.chat.call_args.kwargs["messages"][0]["content"]
    assert prompt.startswith("Use only compact facts.\n<conversation>")
    assert "Historical request" in prompt
    assert "Create a concise structured summary" not in prompt


# 功能：分支摘要成功或截断都持久记录费用，但不创建编码任务或污染上下文正文。
# 设计：真实 Ledger 和 SQLite 接收假模型用量，重开投影后校验会话费用与零 Turn。
@pytest.mark.parametrize("completed", [True, False])
async def test_branch_summary_usage_survives_navigation(tmp_path: Path, completed: bool) -> None:
    runtime_path = tmp_path / "runtime.db"
    runtime = RuntimeService(RuntimeStore(runtime_path), workspace=tmp_path)
    provider = MagicMock()

    # 模拟服务端已经计费后正常完成或被输出限制截断。
    async def chat(**kwargs):
        await kwargs["bus"].publish(LlmUsageEvent(
            run_id=kwargs["run_id"], input_tokens=100, output_tokens=20,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
            model="claude-sonnet-4-6", ts="2026-09-08T08:00:00Z",
        ))
        return LlmResponse(stop_reason="end_turn" if completed else "max_tokens", text="Summary")

    provider.chat = AsyncMock(side_effect=chat)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, MagicMock(), EventBus(), provider=provider,
                             runtime_service=runtime, workspace=tmp_path)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "Question")
    target = store.read_session_events(session.id)[-1].seq
    store.append_message(session.id, "assistant", "Explored code")
    if completed:
        await manager.navigate_tree(session.id, target, summarize=True)
    else:
        with pytest.raises(ValueError, match="did not complete"):
            await manager.navigate_tree(session.id, target, summarize=True)
    reopened = RuntimeService(RuntimeStore(runtime_path), workspace=tmp_path)
    assert await reopened.list_turns(session.id) == []
    usage = await reopened.list_auxiliary_usage(session.id)
    assert len(usage) == 1
    assert usage[0]["input_tokens"] == 100
    assert usage[0]["purpose"] == "branch_summary"
    assert await reopened.get_thread_estimated_cost(session.id) > 0
    ledger = [event for event in store.read_session_events(session.id)
              if event.type == "session.auxiliary_usage"]
    assert len(ledger) == 1
    assert ledger[0].payload["operation_id"] == usage[0]["operation_id"]
    assert "operation_id" not in str(store.read_messages(session.id))
    api = MagicMock()
    api._runtime = reopened
    api_usage = await RuntimeApiService.usage(api)
    assert api_usage["turns"] == 0
    assert api_usage["tokens"]["input_tokens"] == 100
    assert api_usage["cost"] == usage[0]["estimated_cost_usd"]
    app = MagicMock()
    app._runtime = reopened
    app._subagent_registry = app._fleet_registry = None
    tui_usage = await CoreApp._session_usage_summary(app, session.id)
    assert tui_usage["input_tokens"] == 100
    assert tui_usage["estimated_cost_usd"] == api_usage["cost"]
    # 新投影和已有投影各启动两次，模拟重建及正常重启时的重复导入。
    for db_path in (tmp_path / "rebuilt.db", runtime_path):
        for _ in range(2):
            restored_runtime = RuntimeService(RuntimeStore(db_path), workspace=tmp_path)
            restored_manager = SessionManager(
                SessionStore(tmp_path / "sessions"), MagicMock(), EventBus(),
                runtime_service=restored_runtime, workspace=tmp_path,
            )
            await restored_manager.list_sessions()
            restored_usage = await restored_runtime.list_auxiliary_usage(session.id)
            assert len(restored_usage) == 1
            assert restored_usage[0]["operation_id"] == usage[0]["operation_id"]
            assert await restored_runtime.get_thread_estimated_cost(session.id) == api_usage["cost"]
            assert await restored_runtime.list_turns(session.id) == []
            await restored_manager.cancel_all()


# 功能：带摘要导航只总结离开的路径，并在新路径可重放恢复摘要。
# 设计：真实会话写入共享前缀与独立回答，假 Provider 验证请求内容而不消耗费用。
async def test_navigation_summary_is_attached_at_target(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Found auth bug."))
    store = SessionStore(tmp_path)
    manager = SessionManager(store, MagicMock(), EventBus(), provider=provider)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "Shared request")
    target = store.read_session_events(session.id)[-1].seq
    store.append_message(session.id, "assistant", "Investigated auth code")
    before = (store.session_dir(session.id) / "thread.jsonl").read_bytes()
    result = await manager.navigate_tree(session.id, target, summarize=True)
    prompt = provider.chat.call_args.kwargs["messages"][0]["content"]
    assert "Investigated auth code" in prompt
    assert "Shared request" not in prompt
    assert result["editor_text"] == "Shared request"
    assert "Found auth bug." in str(SessionStore(tmp_path).read_messages(session.id))
    assert (store.session_dir(session.id) / "thread.jsonl").read_bytes().startswith(before)


# 功能：截断摘要不能切换会话路径，也不能向模型历史追加半份摘要。
# 设计：比较原始日志前缀与模型历史，允许追加失败审计但禁止切换分支或接纳截断摘要。
async def test_failed_branch_summary_keeps_original_path(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="max_tokens", text="partial"))
    store = SessionStore(tmp_path)
    manager = SessionManager(store, MagicMock(), EventBus(), provider=provider)
    session = await manager.create("chat")
    store.append_message(session.id, "user", "Question")
    target = store.read_session_events(session.id)[-1].seq
    store.append_message(session.id, "assistant", "Answer")
    path = store.session_dir(session.id) / "thread.jsonl"
    before = path.read_bytes()
    before_messages = store.read_messages(session.id)
    with pytest.raises(ValueError, match="did not complete"):
        await manager.navigate_tree(session.id, target, summarize=True)
    assert path.read_bytes().startswith(before)
    assert SessionStore(tmp_path).read_messages(session.id) == before_messages
    events = store.read_session_events(session.id)
    assert [event.type for event in events[-2:]] == [
        "llm.summary_request", "llm.summary_attempt",
    ]
    assert events[-1].payload["response"]["stop_reason"] == "max_tokens"
    assert not any(event.type == "session.branch_selected" for event in events)

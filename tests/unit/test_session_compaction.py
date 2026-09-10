from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.agent_runtime.compaction import prepare_compaction
from code_rook.core.bus.events import LlmTokenEvent
from code_rook.core.compact.compactor import Compactor
from code_rook.core.compact.protocol import SUMMARY_MARKER, validate_tool_protocol
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.budget import clamp_output_token_limit, output_token_budget
from code_rook.core.llm.types import LlmResponse
from code_rook.core.session.store import SessionStore


# 功能：摘要预留量可由 TOML 配置且拒绝非正整数。
# 设计：直接使用生产 TOML 合并入口，排除用户环境与凭据干扰。
def test_compaction_reserve_configuration() -> None:
    from code_rook.core.config import CodeRookConfig, _apply_toml

    config = CodeRookConfig()
    _apply_toml(config, {"compaction": {"reserve_tokens": 2400}})
    assert config.compaction.reserve_tokens == 2400
    for invalid in (0, -1, True, "2400"):
        with pytest.raises(SystemExit, match="reserve_tokens"):
            _apply_toml(config, {"compaction": {"reserve_tokens": invalid}})


# 功能：两类摘要使用不同预算，嵌套压缩不能扩大外层预算且结束后恢复。
# 设计：假 Provider 读取真实异步上下文限额，历史包含旧 Turn 和当前前缀以触发两次请求。
async def test_summary_output_budgets(tmp_path: Path) -> None:
    observed = []

    # 记录三种 Provider 共用的输出上限，不产生真实模型费用。
    async def chat(**kwargs: object) -> LlmResponse:
        observed.append(clamp_output_token_limit(8192))
        return LlmResponse(stop_reason="end_turn", text="Keep API stable.")

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    messages = [
        {"role": "user", "content": "Previous task " * 100},
        {"role": "assistant", "content": "Completed " * 100},
        *_history(),
    ]
    compactor = Compactor(EventBus(), tmp_path, "", keep_recent_tokens=80, reserve_tokens=1000)
    assert await compactor.compact_messages(messages, provider) is not None
    assert observed == [800, 500]
    observed.clear()
    with output_token_budget(300):
        assert await compactor.compact_messages(messages, provider) is not None
        assert clamp_output_token_limit(8192) == 300
    assert observed == [300, 300]
    assert clamp_output_token_limit(8192) == 8192


# 功能：手动压缩消费用户配置的窗口和摘要预算，而非构造器默认值。
# 设计：通过真实 SessionManager 写入短合成会话，默认 20k 窗口不会压缩，配置 80 才能成功。
async def test_manual_compaction_uses_runtime_settings(tmp_path: Path) -> None:
    from code_rook.core.config import CompactionConfig
    from code_rook.core.session.manager import SessionManager

    observed = []

    # 返回极短摘要并捕获实际应用的输出限制。
    async def chat(**kwargs: object) -> LlmResponse:
        observed.append(clamp_output_token_limit(8192))
        return LlmResponse(stop_reason="end_turn", text="Keep API stable.")

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, MagicMock(), EventBus(), provider=provider,
        compaction_config=CompactionConfig(keep_recent_tokens=80, reserve_tokens=1000),
    )
    session = await manager.create("chat")
    for message in _history():
        store.append_message(session.id, message["role"], message["content"])
    await manager.compact(session.id)
    assert observed == [500]
    assert "Keep API stable." in str(store.read_messages(session.id))


# 功能：短会话手动压缩返回无需处理，而不是伪装成 Provider 或压缩故障。
# 设计：不配置 Provider 并保持默认窗口，证明 Core 可仅根据现有上下文安全返回正常状态。
async def test_manual_compaction_reports_short_context_as_not_needed(tmp_path: Path) -> None:
    from code_rook.core.session.manager import SessionManager

    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, MagicMock(), EventBus(), provider=MagicMock())
    session = await manager.create("chat")
    store.append_message(session.id, "user", "hello")

    result = await manager.compact(session.id)

    assert result.status == "not_needed"
    assert result.original_tokens == result.compacted_tokens
    assert result.saved_tokens == 0


# 构造长任务和最近工具闭环，令固定窗口在同一任务中部切开。
def _history() -> list[dict]:
    return [
        {"role": "user", "content": "Keep the API stable. " + "x" * 2000},
        {"role": "assistant", "content": "Located implementation. " + "y" * 2000},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "a.py"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file content " * 40},
            ],
        },
    ]


# 功能：固定窗口切开长任务时保留工具闭环并识别原始任务前缀。
# 设计：让最近工具结果超过窗口预算，检查它和调用仍被完整保留且源消息不变。
def test_prepare_splits_turn_without_splitting_tool_pair() -> None:
    messages = _history()
    original = deepcopy(messages)
    prepared = prepare_compaction(messages, keep_recent_tokens=80)
    assert prepared is not None
    assert prepared.history == []
    assert prepared.turn_prefix == messages[:2]
    assert prepared.recent == messages[2:]
    assert validate_tool_protocol(prepared.recent)[0]
    assert messages == original


# 功能：新摘要采用 Markdown、屏蔽摘要流，并可由追加式日志重建相同上下文。
# 设计：使用真实 SessionStore 和发送 token 的假 Provider，比较压缩前字节前缀与重开投影。
async def test_session_summary_persists_without_streaming_as_answer(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    messages = _history()
    for message in messages:
        store.append_message("sess-1", message["role"], message["content"])
    path = store.session_dir("sess-1") / "thread.jsonl"
    before = path.read_bytes()
    bus = EventBus()
    seen = []

    # 收集前端可见事件以确认摘要文本不会混入正文。
    async def collect(event: object) -> None:
        seen.append(event)

    # 返回前缀摘要，同时模拟真实 Provider 发送流式 token。
    async def chat(**kwargs: object) -> LlmResponse:
        await kwargs["bus"].publish(LlmTokenEvent(run_id="r", token="private summary", ts="t"))
        return LlmResponse(stop_reason="end_turn", text="## Original Request\nKeep the API stable.")

    bus.subscribe(collect)
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    context = ExecutionContext(run_id="r", goal="continue", max_steps=10)
    context.messages = messages
    compactor = Compactor(bus, store.session_dir("sess-1"), "sess-1", store=store, keep_recent_tokens=80)
    result = await compactor.compact(context, provider, trigger="overflow")
    assert result is not None
    assert result.strategy == "session"
    assert "<read-files>\na.py\n</read-files>" in result.summary_text
    assert "PREFIX" in provider.chat.call_args.kwargs["messages"][0]["content"]
    assert context.messages[-2:] == messages[-2:]
    assert path.read_bytes().startswith(before)
    assert SessionStore(tmp_path / "sessions").derive_messages("sess-1") == context.messages
    assert not any(getattr(event, "type", "") == "llm.token" for event in seen)
    assert "Compaction restored" not in str(context.messages)


@pytest.mark.parametrize("stop", ["max_tokens", "failed"])
# 功能：截断或失败的摘要不能作为新上下文覆盖原消息。
# 设计：保留可读但未正常结束的输出，确认判定依据是终止状态而非有无文本。
async def test_incomplete_session_summary_is_not_committed(tmp_path: Path, stop: str) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason=stop, text="partial"))
    context = ExecutionContext(run_id="r", goal="continue", max_steps=10)
    context.messages = _history()
    original = deepcopy(context.messages)
    result = await Compactor(EventBus(), tmp_path, "s", keep_recent_tokens=80).compact(
        context, provider
    )
    assert result is None
    assert context.messages == original


# 功能：再次压缩将旧摘要独立提供给模型而非当作新任务历史。
# 设计：在旧摘要后增加长会话，验证准备结果中的摘要与原始历史分开。
def test_prepare_reuses_previous_summary() -> None:
    prepared = prepare_compaction(
        [{"role": "user", "content": SUMMARY_MARKER + "\nPrevious goal"}, *_history()], 80
    )
    assert prepared is not None
    assert prepared.previous_summary == "Previous goal"
    assert SUMMARY_MARKER not in str(prepared.turn_prefix)

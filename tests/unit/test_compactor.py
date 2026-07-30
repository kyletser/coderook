from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from code_rook.core.compact.compactor import Compactor
from code_rook.core.compact.protocol import SUMMARY_MARKER, validate_tool_protocol
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.session.store import SessionStore


# 构造满足结构化摘要模型的 JSON 响应文本
def _summary_json(
    *,
    goal: str = "Test compaction",
    constraints: list[str] | None = None,
    files: list[dict[str, str]] | None = None,
    todos: list[str] | None = None,
) -> str:
    return json.dumps({
        "goal": goal,
        "completed": ["old work completed"],
        "constraints": constraints or [],
        "decisions": ["retain recent context"],
        "files": files or [],
        "todos": todos or [],
        "errors": [],
        "critical_data": [],
    })


# 构造返回固定结构化摘要的 provider stub
def _stub_provider(summary: str | None = None) -> Any:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text=summary or _summary_json(),
        usage=UsageStats(input_tokens=100, output_tokens=30),
    ))
    return provider


# 构造足够长的多轮纯文本消息以验证保留窗口和压缩收益
def _make_messages(n: int = 8) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for index in range(n):
        messages.append({"role": "user", "content": f"user {index} " + "x" * 400})
        messages.append({"role": "assistant", "content": f"reply {index} " + "y" * 400})
    return messages


# 功能：验证结构化压缩调用不传工具 schema 且仅调用 provider 一次
# 设计：使用合法 JSON stub 隔离真实 API，并检查 chat 调用参数
async def test_compact_messages_calls_provider(tmp_path: Path) -> None:
    provider = _stub_provider()
    compactor = Compactor(EventBus(), tmp_path, "sess-1")

    result = await compactor.compact_messages(_make_messages(), provider)

    assert result is not None
    provider.chat.assert_called_once()
    assert provider.chat.call_args.kwargs["tool_schemas"] == []


# 功能：验证压缩结果包含 Pydantic 摘要和未改写的最近原文窗口
# 设计：比较结果尾部与原消息尾部，确保摘要化没有吞掉近期细节
async def test_compact_preserves_recent_window(tmp_path: Path) -> None:
    messages = _make_messages()
    compactor = Compactor(EventBus(), tmp_path, "sess-1", retain_ratio=0.25)

    result = await compactor.compact_messages(messages, _stub_provider())

    assert result is not None
    assert result.summary.goal == "Test compaction"
    assert result.messages[0]["content"].startswith(SUMMARY_MARKER)
    assert result.retained_messages > 0
    assert result.messages[-result.retained_messages :] == messages[-result.retained_messages :]
    assert result.compacted_tokens < result.original_token_estimate


# 功能：验证自动压缩把摘要和最近窗口一起持久化为 session thread
# 设计：使用真实 SessionStore 检查原子备份与压缩后消息完全一致
async def test_auto_compact_persists_summary_and_recent_window(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.append_message("sess-1", "user", "old message")
    compactor = Compactor(
        EventBus(),
        store.session_dir("sess-1"),
        "sess-1",
        store=store,
    )
    context = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    context.messages = _make_messages()

    result = await compactor.compact(context, _stub_provider())

    assert result is not None
    assert store.read_messages("sess-1") == context.messages
    assert len(context.messages) > 2
    assert list(store.session_dir("sess-1").glob("thread_*.jsonl.bak"))


# 功能：验证压缩摘要文件包含质量分和结构化 Markdown 内容
# 设计：读取真实 summary 文件，断言元数据和目标字段均可人工审计
async def test_compact_writes_auditable_summary_file(tmp_path: Path) -> None:
    context = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    context.messages = _make_messages()

    result = await Compactor(EventBus(), tmp_path, "sess-1").compact(
        context,
        _stub_provider(),
    )

    assert result is not None
    summary_files = list(tmp_path.glob("summary_*.md"))
    assert len(summary_files) == 1
    text = summary_files[0].read_text(encoding="utf-8")
    assert "quality=1.00" in text
    assert "## Goal" in text


# 功能：验证压缩事件暴露触发原因、质量分、保留窗口和摘要路径
# 设计：订阅真实 EventBus 并检查新增字段，覆盖 TUI 所依赖的协议数据
async def test_compact_publishes_observability_event(tmp_path: Path) -> None:
    bus = EventBus()
    received: list[Any] = []

    async def handler(event: Any) -> None:
        received.append(event)

    bus.subscribe(handler)
    context = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    context.messages = _make_messages()

    result = await Compactor(bus, tmp_path, "sess-1").compact(
        context,
        _stub_provider(),
        trigger="auto_threshold",
    )

    assert result is not None
    event = next(item for item in received if item.type == "context.compacted")
    assert event.trigger == "auto_threshold"
    assert event.retained_messages == result.retained_messages
    assert event.quality_score == 1.0
    assert event.summary_path.endswith(".md")


# 功能：验证工具协议不完整时拒绝压缩且不调用摘要模型
# 设计：构造孤立 tool_result，确保压缩器在 LLM 调用前执行协议门禁
async def test_invalid_tool_protocol_preserves_context(tmp_path: Path) -> None:
    provider = _stub_provider()
    messages = [{
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "missing", "content": "x"}],
    }]
    context = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    context.messages = messages

    result = await Compactor(EventBus(), tmp_path, "sess-1").compact(context, provider)

    assert result is None
    assert context.messages == messages
    provider.chat.assert_not_called()


# 功能：验证最近窗口切分不会拆散 tool_use 与 tool_result
# 设计：把工具闭环放在保留边界附近，压缩后再次运行协议校验
async def test_recent_window_keeps_tool_pair_atomic(tmp_path: Path) -> None:
    messages = _make_messages(6)
    messages.extend([
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}],
        },
        {"role": "assistant", "content": "continue " + "z" * 300},
    ])

    result = await Compactor(EventBus(), tmp_path, "sess-1").compact_messages(
        messages,
        _stub_provider(),
    )

    assert result is not None
    valid, errors = validate_tool_protocol(result.messages)
    assert valid, errors


# 功能：验证缺失明确用户约束的摘要不能通过质量门禁
# 设计：源历史包含 must 约束而 stub 返回空 constraints，断言不替换上下文
async def test_quality_gate_rejects_missing_constraint(tmp_path: Path) -> None:
    messages = _make_messages()
    messages[0]["content"] = "You must keep backward compatibility. " + "x" * 400

    result = await Compactor(EventBus(), tmp_path, "sess-1").compact_messages(
        messages,
        _stub_provider(),
    )

    assert result is None


# 功能：验证第二次压缩会把上一版结构化摘要作为输入增量合并
# 设计：完成首轮压缩后追加多轮消息，再检查第二次 provider 请求包含摘要标记
async def test_incremental_compaction_merges_previous_summary(tmp_path: Path) -> None:
    first = await Compactor(EventBus(), tmp_path, "sess-1", retain_ratio=0.2).compact_messages(
        _make_messages(),
        _stub_provider(),
    )
    assert first is not None
    messages = [*first.messages, *_make_messages(6)]
    provider = _stub_provider()

    second = await Compactor(
        EventBus(),
        tmp_path,
        "sess-1",
        retain_ratio=0.2,
    ).compact_messages(messages, provider)

    assert second is not None
    request_text = provider.chat.call_args.kwargs["messages"][0]["content"]
    assert SUMMARY_MARKER in request_text


# 功能：验证 provider 异常时上下文原样保留
# 设计：让摘要调用抛异常，确认失败事务不会产生部分替换
async def test_compact_failure_preserves_context(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("LLM error"))
    context = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    original_messages = _make_messages()
    context.messages = list(original_messages)

    result = await Compactor(EventBus(), tmp_path, "sess-1").compact(context, provider)

    assert result is None
    assert context.messages == original_messages

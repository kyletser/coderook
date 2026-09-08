import re
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.agent_runtime.messages import from_provider, prepare_model_messages
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.tools.registry import ToolRegistry


# 功能：混合用户文本和工具结果的旧历史转成独立消息后，图片与调用 ID 均按目标模型转换。
# 设计：真实循环读取已落账的混合内容，核对消息顺序、工具配对及请求快照，原始账本保持不变。
@pytest.mark.parametrize("supports_images", [False, True])
async def test_mixed_legacy_tool_results_transform_in_order(
    tmp_path: Path, supports_images: bool,
) -> None:
    identifier = "call|" + "x" * 80
    image = {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "pixel-data",
    }}
    history = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": identifier, "name": "read", "input": {"path": "x"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": identifier, "is_error": True,
             "content": [{"type": "text", "text": "Partial read"}, image]},
            {"type": "text", "text": "Inspect this instead"}, image,
        ]},
    ]
    original = deepcopy(history)
    native = from_provider(history)
    assert [message["role"] for message in native] == ["assistant", "toolResult", "user"]
    store = SessionStore(tmp_path / "sessions")
    store.append_messages("sess-mixed-history", history, "previous")
    transcript = SessionTranscriptSink(store, "sess-mixed-history", "current")
    context = ExecutionContext(run_id="current", goal="Continue", max_steps=2)
    context.messages = deepcopy(history)
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Continued"))
    await AgentLoop(
        provider, ToolRegistry(), EventBus(), supports_images=supports_images,
        request_metadata={"wire_format": "anthropic_messages"}, transcript=transcript,
    ).run(context)
    assert context.status == "success", context.reason
    sent = provider.chat.call_args.kwargs["messages"]
    result = sent[1]["content"][0]
    assert result["tool_use_id"] == sent[0]["content"][0]["id"] != identifier
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "Partial read"
    assert sent[2]["content"][0]["text"] == "Inspect this instead"
    if supports_images:
        assert result["content"][1] == image
        assert sent[2]["content"][1] == image
    else:
        assert "pixel-data" not in str(sent)
        assert result["content"][1]["text"].startswith("(tool image omitted:")
        assert sent[2]["content"][1]["text"].startswith("(image omitted:")
    assert list(transcript.latest_request_snapshot().messages) == sent
    assert store.read_messages("sess-mixed-history")[:2] == original
    assert history == original


# 功能：同模型保留思考签名，跨模型转换可读思考并省略加密块，来源字段只留在本地。
# 设计：首轮经真实驱动保存模型来源，重开 Ledger 后用同模型、不同模型及不同路由继续。
@pytest.mark.parametrize("changed", ["same", "model", "route_id"])
async def test_thinking_provenance_survives_model_switch(tmp_path: Path, changed: str) -> None:
    origin = {"wire_format": "anthropic_messages", "model": "claude-sonnet-4-6", "route_id": "one"}
    blocks = [
        {"type": "thinking", "thinking": "Consider the files", "signature": "signed"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn", text="First answer", thinking_blocks=deepcopy(blocks),
    ))
    store = SessionStore(tmp_path / "sessions")
    store.append_message("sess-thinking", "user", "Inspect")
    first = ExecutionContext(run_id="first", goal="Inspect", max_steps=2)
    await AgentLoop(provider, ToolRegistry(), EventBus(), request_metadata=origin,
                    transcript=SessionTranscriptSink(store, "sess-thinking", "first")).run(first)
    assert first.status == "success", first.reason
    history = SessionStore(tmp_path / "sessions").read_messages("sess-thinking")
    assert history[-1]["content"][0]["_coderook_source"] == origin
    assert "_coderook_source" not in str(blocks)
    target = dict(origin)
    if changed != "same":
        target[changed] = "different"
    second_provider = MagicMock()
    second_provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Continued"))
    context = ExecutionContext(run_id="second", goal="Continue", max_steps=2)
    context.messages = deepcopy(history) + [{"role": "user", "content": "Continue"}]
    await AgentLoop(second_provider, ToolRegistry(), EventBus(), request_metadata=target).run(context)
    assert context.status == "success", context.reason
    sent = second_provider.chat.call_args.kwargs["messages"]
    assistant = next(message for message in sent if message["role"] == "assistant")
    assert "_coderook_source" not in str(sent)
    if changed == "same":
        assert assistant["content"][:2] == blocks
    else:
        assert assistant["content"][0] == {"type": "text", "text": "Consider the files"}
        assert "signature" not in str(sent)
        assert "opaque" not in str(sent)
    assert store.read_messages("sess-thinking") == history


# 功能：跨 Provider 长工具 ID 转换后仍一一配对，原始会话不被重写。
# 设计：两个同前缀长 ID 和一个合法 ID 经过生产循环，核对实际请求、快照及原始 Ledger。
async def test_anthropic_history_normalizes_tool_ids_without_rewriting_ledger(tmp_path: Path) -> None:
    ids = ["call_" + "x" * 90 + "|a", "call_" + "x" * 90 + "|b", "toolu_valid-1"]
    history = [
        {"role": "user", "content": "Inspect files"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": identifier, "name": "read", "input": {"path": "x"}}
            for identifier in ids
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": identifier, "content": "File read"}
            for identifier in ids
        ]},
        {"role": "user", "content": "Continue"},
    ]
    original = deepcopy(history)
    store = SessionStore(tmp_path / "sessions")
    store.append_messages("sess-model-switch", history, "previous")
    transcript = SessionTranscriptSink(store, "sess-model-switch", "current")
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Continued"))
    context = ExecutionContext(run_id="current", goal="Continue", max_steps=2)
    context.messages = deepcopy(history)
    await AgentLoop(provider, ToolRegistry(), EventBus(), supports_images=True,
                    request_metadata={"wire_format": "anthropic_messages"},
                    transcript=transcript).run(context)
    assert context.status == "success", context.reason
    sent = provider.chat.call_args.kwargs["messages"]
    converted = [block["id"] for block in sent[1]["content"]]
    assert len(set(converted)) == 3
    assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identifier) for identifier in converted)
    assert converted[-1] == ids[-1]
    assert [block["tool_use_id"] for block in sent[2]["content"]] == converted
    assert list(transcript.latest_request_snapshot().messages) == sent
    assert store.read_messages("sess-model-switch")[:4] == original
    assert context.messages[:4] == original
    assert prepare_model_messages(history, supports_images=True, wire_format="anthropic_messages") == sent
    assert prepare_model_messages(history, supports_images=True, wire_format="openai_responses") == original
    assert history == original

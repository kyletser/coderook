from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from code_rook.core.compact.budget import distill_tool_results, truncate_tool_results
from code_rook.core.llm.types import LlmResponse


def _make_tool_result_msg(content: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "id1", "content": content}],
    }


# 功能：验证 tool_result 内容未超过阈值时原文不变
# 设计：构造 7999 字符内容（刚好低于 8000），断言消息原样返回
def test_short_tool_result_untouched() -> None:
    text = "x" * 7999
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["content"] == text


# 功能：验证 tool_result 内容超过阈值时被截断并附加省略标记
# 设计：构造 10000 字符内容，断言截断后长度 < 原始，且包含省略标记字符串
def test_long_tool_result_truncated() -> None:
    text = "y" * 10_000
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    truncated = result[0]["content"][0]["content"]
    assert len(truncated) < len(text)
    assert "chars omitted" in truncated
    assert truncated.startswith("y" * 2000)
    assert truncated.endswith("y" * 2000)


# 功能：验证 tool_result 内容恰好等于阈值时不截断
# 设计：构造恰好 8000 字符内容，断言原文保持不变
def test_exact_limit_untouched() -> None:
    text = "z" * 8000
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["content"] == text


# 功能：验证 text 类型 block 不受截断影响
# 设计：构造含 text block 的 user 消息，内容超过阈值，断言内容原样返回
def test_non_tool_result_block_untouched() -> None:
    long_text = "a" * 20_000
    msgs = [{"role": "user", "content": [{"type": "text", "text": long_text}]}]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["text"] == long_text


# 功能：验证同一 user 消息含多个 tool_result 时各自独立判断截断
# 设计：构造一条消息含两个 tool_result，一短一长，断言只有长的被截断
def test_multiple_tool_results_independent() -> None:
    short = "s" * 100
    long = "l" * 10_000
    msgs = [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": short},
            {"type": "tool_result", "tool_use_id": "b", "content": long},
        ],
    }]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    blocks = result[0]["content"]
    assert blocks[0]["content"] == short
    assert "chars omitted" in blocks[1]["content"]


# 功能：验证 assistant 消息不被截断处理
# 设计：构造超长内容的 assistant 消息，断言原样返回
def test_assistant_message_untouched() -> None:
    text = "a" * 20_000
    msgs = [{"role": "assistant", "content": text}]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"] == text


# 功能：验证超大工具输出由 LLM 蒸馏并保留来源 ID 与原始长度
# 设计：使用 AsyncMock 返回短摘要，区分蒸馏策略与确定性截断策略
async def test_huge_tool_result_is_distilled() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="42 tests passed"))
    messages = [_make_tool_result_msg("log line\n" * 4000)]

    result, stats = await distill_tool_results(messages, provider, threshold=20_000)

    content = result[0]["content"][0]["content"]
    assert content.startswith("[CodeRook distilled tool output id=id1")
    assert "42 tests passed" in content
    assert stats.distilled == 1
    assert stats.truncated == 0


# 功能：验证工具输出蒸馏失败时自动降级为头尾截断
# 设计：让 provider 抛异常，断言主链仍得到有界输出而不是传播错误
async def test_huge_tool_result_distill_failure_falls_back() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("offline"))
    messages = [_make_tool_result_msg("x" * 30_000)]

    result, stats = await distill_tool_results(
        messages,
        provider,
        threshold=20_000,
        fallback_keep=4_000,
    )

    content = result[0]["content"][0]["content"]
    assert "chars omitted" in content
    assert stats.distilled == 0
    assert stats.truncated == 1

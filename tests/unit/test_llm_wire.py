from __future__ import annotations

from code_rook.core.llm.wire import merge_consecutive_user_messages


# 功能：连续 user 消息被合并为单条，且 tool_result 块与注入文本保持原序
# 设计：用 tool_result 块在前、steering 字符串在后的真实形状验证 Provider 请求归一
def test_merge_consecutive_user_messages_joins_blocks_and_text() -> None:
    messages: list[dict[str, object]] = [
        {"role": "user", "content": "goal"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "f", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        {"role": "user", "content": "steering text"},
    ]
    merged = merge_consecutive_user_messages(messages)
    assert [message["role"] for message in merged] == ["user", "assistant", "user"]
    content = merged[-1]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["type"] == "tool_result"
    assert content[1] == {"type": "text", "text": "steering text"}


# 功能：合并不得修改入参列表，也不得把非 user 角色的相邻消息卷入合并
# 设计：用双 assistant 相邻消息验证只合并 user，避免误伤 Provider 特殊语义
def test_merge_returns_new_list_and_only_touches_user_role() -> None:
    original: list[dict[str, object]] = [
        {"role": "assistant", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "x"},
        {"role": "user", "content": "y"},
    ]
    frozen = [dict(message) for message in original]
    merged = merge_consecutive_user_messages(original)
    assert len(merged) == 3
    assert merged[0]["content"] == "a"
    assert merged[1]["content"] == "b"
    assert merged[2]["content"] == "x\n\ny"
    assert original == frozen

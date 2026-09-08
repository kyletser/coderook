from code_rook.core.agent_runtime.summary_context import FileOperations, serialize_conversation


# 功能：累计文件清单跨摘要保留，修改文件从只读集合移除且路径转义可往返。
# 设计：将旧清单与新调用合并，避免把模型自由摘要是否提到路径当作成功条件。
def test_file_tracking_survives_summary_and_deduplicates() -> None:
    files = FileOperations(read={"a.py", "x&y.py"}, modified={"old.py"})
    restored = FileOperations()
    restored.merge_summary(files.render())
    restored.collect([{"role": "assistant", "content": [
        {"type": "tool_use", "name": "edit", "input": {"path": "a.py"}},
        {"type": "tool_use", "name": "read", "input": {"path": "a.py"}},
    ]}])
    assert restored.render() == (
        "\n\n<read-files>\nx&amp;y.py\n</read-files>"
        "\n\n<modified-files>\na.py\nold.py\n</modified-files>"
    )


# 功能：摘要请求裁剪工具正文且不携带图片与签名的原始协议数据。
# 设计：构造唯一秘密占位符以及长工具结果，检查序列化文本而不修改源消息。
def test_summary_serialization_bounds_tool_results() -> None:
    messages = [
        {"role": "user", "content": [{"type": "image", "source": {"data": "BASE64"}}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Reason", "signature": "SIGNATURE"},
            {"type": "tool_use", "name": "read", "input": {"path": "a.py"}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "content": "x" * 5000}]},
    ]
    serialized = serialize_conversation(messages)
    assert "BASE64" not in serialized and "SIGNATURE" not in serialized
    assert "[Assistant thinking]: Reason" in serialized
    assert "3000 more characters truncated" in serialized
    assert "x" * 2001 not in serialized
    assert len(messages[-1]["content"][0]["content"]) == 5000

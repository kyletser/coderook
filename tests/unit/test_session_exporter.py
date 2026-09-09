from __future__ import annotations

import json

from code_rook.core.session.exporter import export_session
from code_rook.core.session.model import Session


# 构造包含分支来源的固定会话供导出测试复用
def _session() -> Session:
    return Session(
        "sess-export",
        "chat",
        "waiting_for_input",
        "Export title",
        "2026-01-01",
        "2026-01-02",
        ["run-1"],
        parent_session_id="sess-parent",
    )


# 功能：验证 Markdown 导出保留文本、工具、笔记和分支来源
# 设计：组合纯文本和工具块后检查用户可见的关键片段，覆盖主要 Markdown 投影路径
def test_markdown_export_preserves_text_tools_notes_and_lineage() -> None:
    messages = [
        {"role": "user", "content": "Inspect the repo"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "name": "read_file", "input": {"path": "README.md"}},
            ],
        },
    ]

    filename, media_type, content = export_session(
        _session(),
        messages,
        "## Note\nKeep this.",
        "markdown",
    )

    assert filename == "sess-export.md"
    assert media_type == "text/markdown"
    assert "# Export title" in content
    assert "Forked from: `sess-parent`" in content
    assert "Inspect the repo" in content
    assert "Tool call: `read_file`" in content
    assert "Keep this." in content


# 功能：验证 Markdown 导出把思考和图片转成可读内容且不泄露内部来源元数据
# 设计：注入带来源字段的 thinking 与图片块，检查语义化输出并排除原始 JSON 调试字段
def test_markdown_export_renders_reasoning_and_images_without_internal_metadata() -> None:
    _filename, _media_type, content = export_session(
        _session(),
        [{
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "inspect files\nthen answer",
                    "_coderook_source": {"route_id": "private-route"},
                },
                {
                    "type": "image",
                    "source": {"media_type": "image/png", "data": "iVBORw0KGgo="},
                },
            ],
        }],
        "",
        "markdown",
    )

    assert "**Thinking**" in content
    assert "> inspect files\n> then answer" in content
    assert "**Image attachment** · `image/png`" in content
    assert "_coderook_source" not in content
    assert "private-route" not in content


# 功能：验证 JSON 导出保持结构并正确往返 Unicode 内容
# 设计：解析实际导出字符串并逐层断言字段，避免仅靠字符串匹配漏掉结构变化
def test_json_export_is_structured_and_roundtrips_unicode() -> None:
    filename, media_type, content = export_session(
        _session(),
        [{"role": "user", "content": "你好"}],
        "笔记",
        "json",
    )
    payload = json.loads(content)

    assert filename == "sess-export.json"
    assert media_type == "application/json"
    assert payload["schema_version"] == 1
    assert payload["session"]["parent_session_id"] == "sess-parent"
    assert payload["messages"][0]["content"] == "你好"
    assert payload["notes"] == "笔记"


# 功能：验证 HTML 导出可离线展示混合消息且不会执行会话中的标签文本
# 设计：输入脚本标签、工具调用、错误结果和内嵌图片，检查单文件结构、转义与展示语义
def test_html_export_is_self_contained_and_escapes_conversation_content() -> None:
    filename, media_type, content = export_session(
        _session(),
        [
            {"role": "user", "content": "<script>alert('no')</script>"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "check <repo>"},
                    {"type": "tool_use", "name": "read_file", "input": {"path": "a.py"}},
                    {"type": "tool_result", "content": "denied", "is_error": True},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                ],
            },
        ],
        "Keep <private> notes.",
        "html",
    )

    assert filename == "sess-export.html"
    assert media_type == "text/html; charset=utf-8"
    assert content.startswith("<!doctype html>")
    assert "<script>alert" not in content
    assert "&lt;script&gt;alert(&#x27;no&#x27;)&lt;/script&gt;" in content
    assert "Tool call · read_file" in content
    assert '<details class="tool error">' in content
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in content
    assert "https://" not in content and "http://" not in content

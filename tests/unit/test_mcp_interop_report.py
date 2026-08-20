from __future__ import annotations

from scripts.run_mcp_official_interop import _render_markdown


# 功能：验证官方 MCP 互操作报告完整呈现三个 transport 的五类结果
# 设计：用一个通过和一个失败结果固定 yes/no 语义，避免 Markdown 只展示总状态而隐藏部分能力失败
def test_render_mcp_interop_markdown_preserves_capability_matrix() -> None:
    report = {
        "generated_at": "2026-08-20T00:00:00+00:00",
        "commit": "abc123",
        "official_sdk": "mcp[cli]==2.0.0",
        "platform": "windows",
        "results": [
            {
                "transport": "stdio",
                "status": "passed",
                "tools": True,
                "resources": True,
                "prompts": True,
                "cancellation": True,
                "reconnect": True,
            },
            {"transport": "sse", "status": "failed"},
        ],
    }

    rendered = _render_markdown(report)

    assert "| stdio | passed | yes | yes | yes | yes | yes |" in rendered
    assert "| sse | failed | no | no | no | no | no |" in rendered
    assert "does not certify arbitrary third-party MCP servers" in rendered

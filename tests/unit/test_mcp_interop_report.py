from __future__ import annotations

from pathlib import Path

from scripts.check_public_repo import find_mcp_evidence_contract_issues
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


# 功能：验证 checked-in 官方 MCP JSON 绑定完整 commit 且三 transport 五类能力均通过
# 设计：直接校验 runner 产出的机器报告而非 Markdown 摘要，防止人工修改表格掩盖底层失败字段
def test_checked_in_mcp_official_evidence_is_complete() -> None:
    root = Path(__file__).resolve().parents[2]

    assert find_mcp_evidence_contract_issues(root) == []

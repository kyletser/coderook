from __future__ import annotations

import sys
from pathlib import Path

from examples.automated_fix import build_command as build_fix_command
from examples.read_only_review import build_command as build_review_command

from code_rook.core.mcp.client import McpClient

_MCP_SERVER = Path(__file__).resolve().parents[2] / "examples" / "mcp_echo_server.py"


# 功能：验证只读示例仅显式放行读取、搜索和 diff 工具
# 设计：检查构造后的 argv 而不调用模型，确保文档示例可在普通 CI 中确定性验证
def test_read_only_review_example_has_no_write_allowlist() -> None:
    command = build_review_command("review")

    assert "edit_file" not in command
    assert "apply_patch" not in command
    assert command.count("--allow-tool") == 5
    assert "Repository" in command
    assert "stream-json" in command


# 功能：验证自动修复示例显式声明编辑、验证和有限提问策略
# 设计：直接检查 argv 中的关键工具与 preset 参数，避免启动 daemon 或消耗模型费用
def test_automated_fix_example_declares_controlled_tools() -> None:
    command = build_fix_command("fix")

    assert "edit_file" in command
    assert "apply_patch" in command
    assert "Bash.run" in command
    assert command[command.index("--question-mode") + 1] == "preset"


# 功能：验证最小 MCP 示例能被真实 CodeRook stdio 客户端发现并调用
# 设计：启动标准库子进程完成 initialize、tools/list 和 tools/call，覆盖真实换行帧与进程回收
async def test_mcp_echo_example_roundtrip() -> None:
    client = McpClient()
    await client.connect_stdio(sys.executable, [str(_MCP_SERVER)])
    try:
        tools = await client.list_tools()
        result = await client.call_tool("echo", {"text": "hello"})
    finally:
        await client.close()

    assert [tool.name for tool in tools] == ["echo"]
    assert result == "hello"

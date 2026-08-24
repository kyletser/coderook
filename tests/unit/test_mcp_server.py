from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.config import McpServerConfig
from code_rook.core.mcp.client import McpClient
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.processes import ProcessSupervisor


# 构造声明 Tools、Resources 与 Prompts 的最小 MCP client 替身
def _capable_client() -> SimpleNamespace:
    return SimpleNamespace(
        server_capabilities={"resources": {}, "prompts": {}},
        list_tools=AsyncMock(return_value=[]),
        list_resources=AsyncMock(return_value=[]),
        list_prompts=AsyncMock(return_value=[]),
        start_server_stream=MagicMock(),
    )


# 功能：验证 Labs 关闭时 MCP 只发现稳定 Tools 而不调用 Resources/Prompts 协议
# 设计：服务端同时声明两项实验能力，用调用计数证明空结果不是被发现后再隐藏
async def test_mcp_resources_and_prompts_are_not_discovered_without_labs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _capable_client()
    manager = McpServerManager(enable_labs=False)
    monkeypatch.setattr(manager, "_connect", AsyncMock(return_value=client))

    await manager.start_all([McpServerConfig(name="labs-off", transport="tcp")])

    client.list_tools.assert_awaited_once()
    client.list_resources.assert_not_awaited()
    client.list_prompts.assert_not_awaited()
    assert manager.describe()[0]["resources"] == []
    assert manager.describe()[0]["prompts"] == []


# 功能：验证显式开启 Labs 后 MCP Resources 与 Prompts 才进入发现流程
# 设计：与关闭用例复用同一能力声明，确保唯一变量是 daemon 冻结的 Labs 开关
async def test_mcp_resources_and_prompts_are_discovered_with_labs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _capable_client()
    manager = McpServerManager(enable_labs=True)
    monkeypatch.setattr(manager, "_connect", AsyncMock(return_value=client))

    await manager.start_all([McpServerConfig(name="labs-on", transport="tcp")])

    client.list_resources.assert_awaited_once()
    client.list_prompts.assert_awaited_once()


# 功能：验证 MCP stdio 仅继承脱敏基础环境和用户显式交给该扩展的变量
# 设计：捕获真实 connect_stdio 交给 supervisor 的 env，绕过协议 IO 并核对 ambient/explicit 边界
async def test_mcp_stdio_environment_does_not_inherit_ambient_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret")
    process = SimpleNamespace(
        stdout=asyncio.StreamReader(),
        stderr=asyncio.StreamReader(),
        stdin=MagicMock(),
    )
    process.stderr.feed_eof()
    supervisor = MagicMock(spec=ProcessSupervisor)
    supervisor.start_exec = AsyncMock(return_value=process)
    client = McpClient(supervisor)
    monkeypatch.setattr(client, "_initialize", AsyncMock())

    await client.connect_stdio(
        "mcp-server",
        [],
        {"MCP_EXPLICIT_TOKEN": "explicit-secret"},
    )
    assert client._stderr_task is not None
    await client._stderr_task

    environment = supervisor.start_exec.await_args.kwargs["env"]
    assert "GITHUB_TOKEN" not in environment
    assert environment["MCP_EXPLICIT_TOKEN"] == "explicit-secret"

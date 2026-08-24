from __future__ import annotations

import logging
import os
from typing import Any

from code_rook.core.config import McpServerConfig
from code_rook.core.mcp.client import McpClient
from code_rook.core.mcp.tool import McpTool
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# 管理所有 MCP server 连接的生命周期：启动、工具发现、注册、状态快照、关闭
class McpServerManager:
    # 初始化多 server 状态并保存共享进程监督器
    def __init__(
        self,
        process_supervisor: ProcessSupervisor | None = None,
        *,
        enable_labs: bool = False,
    ) -> None:
        self._clients: dict[str, McpClient] = {}
        self._tools: list[McpTool] = []
        # 每个 server 的元数据快照：status/error/tools，便于 mcp.list 查询
        self._states: dict[str, dict[str, Any]] = {}
        self._process_supervisor = process_supervisor
        self._enable_labs = enable_labs

    # 依次连接每个 MCP server，发现工具后缓存供后续 registry 使用；失败时记录日志并跳过
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        for cfg in servers:
            try:
                client = await self._connect(cfg)
                tool_defs = await client.list_tools()
                resource_defs = []
                prompt_defs = []
                if self._enable_labs and "resources" in client.server_capabilities:
                    try:
                        resource_defs = await client.list_resources()
                    except Exception:
                        log.warning("mcp: resources/list failed for '%s'", cfg.name)
                if self._enable_labs and "prompts" in client.server_capabilities:
                    try:
                        prompt_defs = await client.list_prompts()
                    except Exception:
                        log.warning("mcp: prompts/list failed for '%s'", cfg.name)
                if cfg.transport == "streamable_http":
                    client.start_server_stream()
                for tool_def in tool_defs:
                    self._tools.append(McpTool(client, cfg.name, tool_def))
                self._clients[cfg.name] = client
                self._states[cfg.name] = {
                    "name": cfg.name,
                    "transport": cfg.transport,
                    "status": "connected",
                    "tools": [
                        {
                            "name": tool_def.name,
                            "description": tool_def.description or "",
                        }
                        for tool_def in tool_defs
                    ],
                    "resources": [
                        {
                            "uri": resource.uri,
                            "name": resource.name,
                            "description": resource.description,
                            "mime_type": resource.mime_type,
                        }
                        for resource in resource_defs
                    ],
                    "prompts": [
                        {
                            "name": prompt.name,
                            "description": prompt.description,
                            "arguments": prompt.arguments,
                        }
                        for prompt in prompt_defs
                    ],
                    "error": "",
                }
                log.info(
                    "mcp: server '%s' connected, %d tool(s) discovered",
                    cfg.name, len(tool_defs),
                )
            except Exception as exc:
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)
                self._states[cfg.name] = {
                    "name": cfg.name,
                    "transport": cfg.transport,
                    "status": "failed",
                    "tools": [],
                    "resources": [],
                    "prompts": [],
                    "error": str(exc)[:500],
                }

    # 将所有已发现的 MCP 工具注册到指定 registry
    def register_tools(self, registry: ToolRegistry) -> None:
        for tool in self._tools:
            registry.register(tool)

    # 返回已发现的 MCP 工具列表（用于 runner 每次 run 时注入新 registry）
    def get_tools(self) -> list[McpTool]:
        return list(self._tools)

    # 返回每个 server 的名称/传输/状态/工具数与失败原因快照
    def describe(self) -> list[dict[str, Any]]:
        return [dict(state) for state in self._states.values()]

    # 关闭所有 MCP 连接并终止 stdio 子进程
    async def stop_all(self) -> None:
        for name, client in list(self._clients.items()):
            try:
                await client.close()
                log.info("mcp: server '%s' closed", name)
            except Exception:
                log.warning("mcp: error closing server '%s'", name)
        self._clients.clear()

    # 根据 transport 类型建立连接
    async def _connect(self, cfg: McpServerConfig) -> McpClient:
        client = McpClient(self._process_supervisor)
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"mcp server '{cfg.name}': stdio transport requires 'command'")
            await client.connect_stdio(cfg.command, cfg.args, cfg.env or None)
        elif cfg.transport == "tcp":
            await client.connect_tcp(cfg.host, cfg.port)
        elif cfg.transport == "streamable_http":
            if not cfg.url:
                raise ValueError(
                    f"mcp server '{cfg.name}': streamable_http transport requires 'url'"
                )
            headers: dict[str, str] = {}
            if cfg.auth_token_env:
                token = os.environ.get(cfg.auth_token_env, "")
                if not token:
                    raise ValueError(
                        f"mcp server '{cfg.name}': auth token environment variable is missing"
                    )
                headers["Authorization"] = f"Bearer {token}"
            await client.connect_streamable_http(cfg.url, headers=headers)
        else:
            raise ValueError(f"mcp server '{cfg.name}': unknown transport '{cfg.transport}'")
        return client

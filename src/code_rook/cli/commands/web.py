from __future__ import annotations

import asyncio
import os
import webbrowser
from pathlib import Path

from code_rook.cli.commands.core import ensure_core_running
from code_rook.core.config import CodeRookConfig
from code_rook.core.projects import ProjectRegistry
from code_rook.core.transport.socket_client import SocketClient


# 通过已认证 IPC 获取固定的本机 Web 地址
async def _request_launch_url(config: CodeRookConfig) -> str:
    client = SocketClient.from_config(config)
    await client.connect()
    event_loop = asyncio.create_task(client.run_event_loop(), name="web-launch-ipc")
    try:
        result = await asyncio.wait_for(
            client.send_command("web.launch", {}),
            timeout=5.0,
        )
        return str(result["url"])
    finally:
        event_loop.cancel()
        await asyncio.gather(event_loop, return_exceptions=True)
        await client.close()


# 启动或复用当前工作区 Core，并按用户选择打开本地 Web 工作区
def cmd_web(
    config: CodeRookConfig,
    *,
    no_open: bool = False,
    env_file: Path | None = None,
    explicit_workspace: bool = False,
) -> int:
    current = Path.cwd().resolve()
    registry = ProjectRegistry()
    if registry.is_protected_workspace(current):
        if explicit_workspace:
            raise ValueError("CodeRook's internal source cannot be opened as a project")
        os.chdir(registry.prepare_welcome_workspace())
    ensure_core_running(config, env_file=env_file)
    url = asyncio.run(_request_launch_url(config))
    opened = False if no_open else webbrowser.open(url, new=2)
    if no_open or not opened:
        print(url)
    else:
        print("CodeRook Web opened in your browser.")
    return 0

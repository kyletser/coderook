from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Literal

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient

MemoryAction = Literal[
    "list",
    "add",
    "edit",
    "pin",
    "unpin",
    "expire",
    "delete",
    "auto",
]


# 连接 daemon 并执行一条 typed memory 命令
async def _memory_async(
    config: CodeRookConfig,
    method: str,
    params: dict[str, object],
) -> dict[str, Any]:
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        return await client.send_command(method, params)
    finally:
        loop_task.cancel()
        await client.close()


# 输出项目记忆管理结果并为脚本调用返回稳定退出码
def cmd_memory(
    config: CodeRookConfig,
    action: MemoryAction,
    *,
    params: dict[str, object] | None = None,
    confirmed: bool = False,
    as_json: bool = False,
) -> int:
    if action == "delete" and not confirmed:
        print("error: memory delete requires --yes", file=sys.stderr)
        return 2
    methods = {
        "list": "memory.list",
        "add": "memory.add",
        "edit": "memory.edit",
        "pin": "memory.pin",
        "unpin": "memory.pin",
        "expire": "memory.expire",
        "delete": "memory.delete",
        "auto": "memory.settings.set",
    }
    payload = dict(params or {})
    if action == "unpin":
        payload["pinned"] = False
    elif action == "pin":
        payload["pinned"] = True
    try:
        result = asyncio.run(_memory_async(config, methods[action], payload))
    except (ConnectionRefusedError, OSError, IpcTokenError, IpcError) as exc:
        print(f"error: memory command failed: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if action == "list":
        settings = result.get("settings", {})
        mode = settings.get("auto_save", "prompt") if isinstance(settings, dict) else "prompt"
        print(f"agent_auto_save={mode}")
        memories = result.get("memories", [])
        for raw in memories if isinstance(memories, list) else []:
            if not isinstance(raw, dict):
                continue
            flags = []
            if raw.get("pinned"):
                flags.append("pinned")
            if raw.get("expired"):
                flags.append("expired")
            print(
                f"{raw.get('id', '')}  [{raw.get('type', '')}] "
                f"{raw.get('name', '')}  {','.join(flags) or '-'}"
            )
        return 0
    if action == "delete":
        deleted = bool(result.get("deleted"))
        print("deleted" if deleted else "not found")
        return 0 if deleted else 1
    if action == "auto":
        settings = result.get("settings", {})
        mode = settings.get("auto_save", "") if isinstance(settings, dict) else ""
        print(f"agent_auto_save={mode}")
        return 0
    memory = result.get("memory", {})
    memory_id = memory.get("id", "") if isinstance(memory, dict) else ""
    print(f"{action} memory_id={memory_id}")
    return 0

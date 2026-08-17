"""IPC 动作封装层：把 App 中分散的 send_command 调用收敛于此，统一超时与错误文案。

每个 helper 只负责「发命令并返回结果」，不涉及任何渲染；超时与传输异常统一
转化为 `IpcActionError`，供调用方决定如何向用户提示。
"""

from __future__ import annotations

import asyncio
from typing import Any

from code_rook.core.transport.socket_client import IpcError, SocketClient


# 封装层统一抛出的 IPC 动作错误，message 为可直接展示给用户的提示文案
class IpcActionError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# 底层统一发送：对 send_command 加超时，并把超时/传输异常转为 IpcActionError
async def send(
    client: SocketClient,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            client.send_command(method, params),
            timeout=timeout,
        )
    except TimeoutError as exc:
        raise IpcActionError("IPC 请求超时") from exc
    except IpcError as exc:
        raise IpcActionError(str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise IpcActionError(str(exc)) from exc


# 手动压缩当前会话上下文，返回 compaction 结果
async def compact(client: SocketClient, session_id: str) -> dict[str, Any]:
    return await send(
        client,
        "session.compact",
        {"session_id": session_id, "focus": ""},
    )


# 加载当前会话最近一次 run 的任务列表
async def get_tasks(
    client: SocketClient, session_id: str
) -> list[dict[str, Any]]:
    result = await send(client, "session.tasks", {"session_id": session_id})
    return list(result.get("tasks", []))


# 加载全部持久 Worker / Fleet 状态列表
async def get_workers(client: SocketClient) -> dict[str, Any]:
    return await send(client, "worker.list", {"limit": 50})


# 按 id 加载单个 durable workflow 投影
async def get_workflow(
    client: SocketClient, workflow_id: str
) -> dict[str, Any]:
    return await send(
        client,
        "workflow.get",
        {"workflow_id": workflow_id},
    )


# 加载 durable workflow 列表
async def list_workflows(client: SocketClient) -> dict[str, Any]:
    return await send(client, "workflow.list", {"limit": 50})


# 通过 typed IPC 启动持久 workflow 并返回 workflow_id
async def start_workflow(
    client: SocketClient,
    source: str,
    format_name: str,
) -> dict[str, Any]:
    return await send(
        client,
        "workflow.start",
        {"source": source, "format": format_name},
    )


# 读取当前工作区统一 diff（scope=all, path=.）
async def get_diff(client: SocketClient) -> dict[str, Any]:
    return await send(
        client,
        "workspace.diff",
        {"scope": "all", "path": "."},
    )


# 列出会话全部 checkpoint（是否可用交由调用方过滤）
async def list_checkpoints(
    client: SocketClient, session_id: str
) -> list[dict[str, Any]]:
    result = await send(
        client,
        "session.checkpoints",
        {"session_id": session_id},
    )
    return list(result.get("checkpoints", []))


# 将会话回滚到指定 checkpoint
async def rewind(
    client: SocketClient,
    session_id: str,
    checkpoint_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "session.rewind",
        {"session_id": session_id, "checkpoint_id": checkpoint_id},
    )


# 读取当前会话上下文估算与占用
async def get_context(
    client: SocketClient, session_id: str
) -> dict[str, Any]:
    return await send(
        client,
        "session.context",
        {"session_id": session_id},
    )


# 按 turn_id 加载 durable inspector 投影
async def inspect_turn(client: SocketClient, turn_id: str) -> dict[str, Any]:
    return await send(client, "turn.inspect", {"turn_id": turn_id})


# 读取当前会话的 authority 快照
async def get_authority_snapshot(
    client: SocketClient, session_id: str
) -> dict[str, Any]:
    return await send(
        client,
        "session.get_authority",
        {"session_id": session_id},
    )


# 覆盖式设置会话的 authority 维度（profile/mode/workspace_trust 至少一项）
async def set_authority(
    client: SocketClient,
    session_id: str,
    *,
    profile: str | None = None,
    mode: str | None = None,
    workspace_trust: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": session_id}
    if profile is not None:
        params["profile"] = profile
    if mode is not None:
        params["mode"] = mode
    if workspace_trust is not None:
        params["workspace_trust"] = workspace_trust
    return await send(
        client,
        "session.set_authority",
        params,
    )


# 列出全部 MCP server 的状态与工具清单
async def list_mcp_servers(client: SocketClient) -> dict[str, Any]:
    return await send(client, "mcp.list", {})


# 列出 hook 配置表与最近执行记录，limit 控制审计条数
async def list_hooks(
    client: SocketClient, *, limit: int = 20
) -> dict[str, Any]:
    return await send(client, "hooks.list", {"limit": limit})


# 手动重跑指定 id 的历史 hook
async def rerun_hook(client: SocketClient, hook_id: str) -> dict[str, Any]:
    return await send(client, "hooks.rerun", {"hook_id": hook_id})


# 列出当前项目记忆条目
async def list_memories(client: SocketClient) -> dict[str, Any]:
    return await send(client, "memory.list", {})


# 删除指定 id 的一条项目记忆
async def delete_memory(client: SocketClient, memory_id: str) -> dict[str, Any]:
    return await send(client, "memory.delete", {"memory_id": memory_id})


# 查询后台 shell 任务列表，或单个任务的全量输出
async def get_background(
    client: SocketClient, *, job_id: str = ""
) -> dict[str, Any]:
    params: dict[str, Any] = {"job_id": job_id}
    return await send(client, "background.get", params)


# 取消指定后台 shell 任务
async def cancel_background(client: SocketClient, job_id: str) -> dict[str, Any]:
    return await send(client, "background.cancel", {"job_id": job_id})


# 取消指定持久 Worker/子代理任务
async def cancel_worker(client: SocketClient, worker_id: str) -> dict[str, Any]:
    return await send(client, "worker.cancel", {"worker_id": worker_id})
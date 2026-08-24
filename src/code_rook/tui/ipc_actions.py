"""IPC 动作封装层：把 App 中分散的 send_command 调用收敛于此，统一超时与错误文案。

每个 helper 只负责「发命令并返回结果」，不涉及任何渲染；超时与传输异常统一
转化为 `IpcActionError`，供调用方决定如何向用户提示。
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

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


# 向 Core 持久提交当前计划的批准、修改或取消决定
async def respond_plan(
    client: SocketClient,
    session_id: str,
    run_id: str,
    decision: Literal["approve", "revise", "cancel"],
    *,
    revision: str = "",
) -> dict[str, Any]:
    return await send(
        client,
        "plan.respond",
        {
            "session_id": session_id,
            "run_id": run_id,
            "decision": decision,
            "revision": revision,
        },
    )


# 加载当前会话最近一次 run 的任务列表
async def get_tasks(
    client: SocketClient, session_id: str
) -> list[dict[str, Any]]:
    result = await send(client, "session.tasks", {"session_id": session_id})
    return list(result.get("tasks", []))


# 加载当前会话的全部持久 Worker 状态列表
async def get_workers(
    client: SocketClient,
    session_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.list",
        {"limit": 50, "session_id": session_id},
    )


# 通过 daemon-owned launcher 启动新的持久 Worker
async def start_worker(
    client: SocketClient,
    session_id: str,
    *,
    description: str,
    prompt: str,
    profile: str = "",
    route_id: str = "",
    model: str = "",
    read_only: bool = True,
    exact_files: list[str] | None = None,
    write_roots: list[str] | None = None,
    token_budget: int | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "session_id": session_id,
        "description": description,
        "prompt": prompt,
        "profile": profile,
        "route_id": route_id,
        "model": model,
        "read_only": read_only,
        "exact_files": list(exact_files or []),
        "write_roots": list(write_roots or []),
    }
    if token_budget is not None:
        params["token_budget"] = token_budget
    return await send(client, "worker.start", params)


# 查询严格绑定当前会话的单个 Worker 状态
async def get_worker_status(
    client: SocketClient,
    session_id: str,
    worker_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.status",
        {"session_id": session_id, "worker_id": worker_id},
    )


# 按原 WorkerRecord 的冻结边界启动下一次 attempt
async def retry_worker(
    client: SocketClient,
    session_id: str,
    worker_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.retry",
        {"session_id": session_id, "worker_id": worker_id},
    )


# 按持久游标读取单个 Worker 的有界进度事件
async def get_worker_events(
    client: SocketClient,
    session_id: str,
    worker_id: str,
    *,
    after_cursor: int = 0,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.events",
        {
            "session_id": session_id,
            "worker_id": worker_id,
            "after_cursor": after_cursor,
            "limit": 50,
        },
    )


# 向仍运行的 Worker 发送有界 followup 指令
async def followup_worker(
    client: SocketClient,
    session_id: str,
    worker_id: str,
    message: str,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.followup",
        {"session_id": session_id, "worker_id": worker_id, "message": message},
    )


# 人工审查 Worker handoff，但不触发 apply 或 merge
async def review_worker(
    client: SocketClient,
    session_id: str,
    worker_id: str,
    *,
    approved: bool,
    confirmed: bool = False,
    expected_digest: str = "",
) -> dict[str, Any]:
    return await send(
        client,
        "worker.review",
        {
            "session_id": session_id,
            "worker_id": worker_id,
            "approved": approved,
            "confirmed": confirmed,
            "expected_digest": expected_digest,
        },
    )


# 应用已经人工批准且由 daemon 验证的 Worker handoff
async def apply_worker(
    client: SocketClient,
    session_id: str,
    worker_id: str,
    expected_digest: str,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.apply",
        {
            "session_id": session_id,
            "worker_id": worker_id,
            "expected_digest": expected_digest,
            "confirmed": True,
        },
    )


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


# 将用户显式选定的当前改动加入 Git index
async def stage_changes(
    client: SocketClient,
    session_id: str,
    paths: list[str],
    expected_digest: str,
) -> dict[str, Any]:
    return await send(
        client,
        "workspace.stage",
        {
            "session_id": session_id,
            "paths": paths,
            "expected_digest": expected_digest,
            "confirmed": True,
        },
    )


# 从已 stage 改动创建本地 commit，服务端不会执行 hooks 或 push
async def commit_changes(
    client: SocketClient,
    session_id: str,
    message: str,
    expected_digest: str,
) -> dict[str, Any]:
    return await send(
        client,
        "workspace.commit",
        {
            "session_id": session_id,
            "message": message,
            "expected_digest": expected_digest,
            "confirmed": True,
        },
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


# 读取指定 checkpoint 的恢复范围和可重验摘要
async def preview_rewind(
    client: SocketClient,
    session_id: str,
    checkpoint_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "session.rewind_preview",
        {"session_id": session_id, "checkpoint_id": checkpoint_id},
    )


# 携带已审查摘要和显式确认将会话回滚到指定 checkpoint
async def rewind(
    client: SocketClient,
    session_id: str,
    checkpoint_id: str,
    expected_digest: str,
) -> dict[str, Any]:
    return await send(
        client,
        "session.rewind",
        {
            "session_id": session_id,
            "checkpoint_id": checkpoint_id,
            "expected_digest": expected_digest,
            "confirmed": True,
        },
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


# 手动新增一条项目记忆
async def add_memory(
    client: SocketClient,
    *,
    name: str,
    body: str,
    description: str = "",
    memory_type: str = "project",
    source_session_id: str = "",
) -> dict[str, Any]:
    return await send(
        client,
        "memory.add",
        {
            "name": name,
            "body": body,
            "description": description,
            "memory_type": memory_type,
            "source_session_id": source_session_id,
        },
    )


# 修改指定项目记忆的一个或多个字段
async def edit_memory(
    client: SocketClient,
    memory_id: str,
    **changes: object,
) -> dict[str, Any]:
    return await send(client, "memory.edit", {"memory_id": memory_id, **changes})


# 固定或取消固定一条项目记忆
async def pin_memory(
    client: SocketClient,
    memory_id: str,
    *,
    pinned: bool,
) -> dict[str, Any]:
    return await send(
        client,
        "memory.pin",
        {"memory_id": memory_id, "pinned": pinned},
    )


# 设置或清除一条项目记忆的过期时间
async def expire_memory(
    client: SocketClient,
    memory_id: str,
    *,
    expires_at: str | None,
) -> dict[str, Any]:
    return await send(
        client,
        "memory.expire",
        {"memory_id": memory_id, "expires_at": expires_at},
    )


# 删除指定 id 的一条项目记忆
async def delete_memory(client: SocketClient, memory_id: str) -> dict[str, Any]:
    return await send(client, "memory.delete", {"memory_id": memory_id})


# 更新 Agent 自动保存记忆的策略
async def set_memory_auto_save(
    client: SocketClient,
    mode: str,
) -> dict[str, Any]:
    return await send(client, "memory.settings.set", {"auto_save": mode})


# 查询后台 shell 任务列表，或单个任务的全量输出
async def get_background(
    client: SocketClient,
    session_id: str,
    *,
    job_id: str = "",
) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": session_id, "job_id": job_id}
    return await send(client, "background.get", params)


# 取消指定后台 shell 任务
async def cancel_background(
    client: SocketClient,
    session_id: str,
    job_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "background.cancel",
        {"session_id": session_id, "job_id": job_id},
    )


# 取消指定持久 Worker/子代理任务
async def cancel_worker(
    client: SocketClient,
    session_id: str,
    worker_id: str,
) -> dict[str, Any]:
    return await send(
        client,
        "worker.cancel",
        {"session_id": session_id, "worker_id": worker_id},
    )


# 列出 daemon ArtifactStore 的引用状态与可回收空间
async def list_artifacts(client: SocketClient, *, days: int = 30) -> dict[str, Any]:
    return await send(client, "artifact.list", {"days": days})


# 预览或确认执行引用感知的 Artifact GC
async def gc_artifacts(
    client: SocketClient,
    *,
    days: int = 30,
    confirmed: bool = False,
) -> dict[str, Any]:
    return await send(
        client,
        "artifact.gc",
        {"days": days, "confirmed": confirmed},
    )

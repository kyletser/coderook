from __future__ import annotations

import asyncio
from typing import Any

import pytest

from code_rook.core.transport.socket_client import IpcError
from code_rook.tui import ipc_actions
from code_rook.tui.ipc_actions import IpcActionError


# 记录命令名与参数、按方法返回固定 payload 的 fake client
class _FakeClient:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses or {}

    # 记录本次调用并返回预设 payload（未命中则返回空 dict）
    async def send_command(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        return dict(self.responses.get(method, {}))


# send_command 永远挂起的客户端，用于触发 wait_for 超时
class _HangingClient:
    # 挂起等待，等待外层 wait_for 超时取消
    async def send_command(
        self, _method: str, _params: dict[str, Any]
    ) -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}


# send_command 立即抛出指定异常的客户端（IpcError / RuntimeError）
class _ErrorClient:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    # 把预设异常直接抛出以验证封装层转换
    async def send_command(
        self, _method: str, _params: dict[str, Any]
    ) -> dict[str, Any]:
        raise self.exc


# 功能：验证 compact 返回 compaction 结果且透传 focus 参数
# 设计：fake 记录调用并返回固定 payload，断言方法名与参数完全一致
async def test_compact_success() -> None:
    client = _FakeClient({"session.compact": {"summary_tokens": 10}})
    result = await ipc_actions.compact(client, "sess-1")
    assert result["summary_tokens"] == 10
    assert client.calls == [
        ("session.compact", {"session_id": "sess-1", "focus": ""})
    ]


# 功能：验证 get_tasks 返回任务列表（已抽取 tasks 字段）
# 设计：fake 返回 tasks 数组，断言入参只有 session_id 且返回值就是列表
async def test_get_tasks_success() -> None:
    client = _FakeClient({"session.tasks": {"tasks": [{"id": 1}]}})
    result = await ipc_actions.get_tasks(client, "sess-1")
    assert result == [{"id": 1}]
    assert client.calls == [("session.tasks", {"session_id": "sess-1"})]


# 功能：验证 get_workers 返回 worker.list 结果
# 设计：断言方法名与 limit=50 参数固定，返回原始结果 dict
async def test_get_workers_success() -> None:
    client = _FakeClient({"worker.list": {"workers": [{"worker_id": "w"}]}})
    result = await ipc_actions.get_workers(client)
    assert result["workers"][0]["worker_id"] == "w"
    assert client.calls == [("worker.list", {"limit": 50})]


# 功能：验证 get_workflow 返回单个 workflow 投影
# 设计：断言按 workflow_id 调用 workflow.get
async def test_get_workflow_success() -> None:
    client = _FakeClient({"workflow.get": {"workflow": {"id": "wf"}}})
    result = await ipc_actions.get_workflow(client, "wf-1")
    assert result["workflow"]["id"] == "wf"
    assert client.calls == [("workflow.get", {"workflow_id": "wf-1"})]


# 功能：验证 list_workflows 返回 workflow 列表
# 设计：断言固定 limit=50 参数调用 workflow.list
async def test_list_workflows_success() -> None:
    client = _FakeClient({"workflow.list": {"workflows": []}})
    result = await ipc_actions.list_workflows(client)
    assert result["workflows"] == []
    assert client.calls == [("workflow.list", {"limit": 50})]


# 功能：验证 start_workflow 携带 source 与 format 启动 workflow
# 设计：raw 路径解析由上层完成，封装层只需透传 source/format 字符串
async def test_start_workflow_success() -> None:
    client = _FakeClient({"workflow.start": {"workflow_id": "wf-2"}})
    result = await ipc_actions.start_workflow(client, '{"a":1}', "json")
    assert result["workflow_id"] == "wf-2"
    assert client.calls == [
        ("workflow.start", {"source": '{"a":1}', "format": "json"})
    ]


# 功能：验证 get_diff 按固定 scope/path 调用 workspace.diff
# 设计：diff 的 scope 语义固定为 all，. 表示整个工作区
async def test_get_diff_success() -> None:
    client = _FakeClient({"workspace.diff": {"payload": {"files": []}}})
    result = await ipc_actions.get_diff(client)
    assert result["payload"]["files"] == []
    assert client.calls == [
        ("workspace.diff", {"scope": "all", "path": "."})
    ]


# 功能：验证 list_checkpoints 返回会话 checkpoint 列表（是否可用交由上层过滤）
# 设计：抽出 checkpoints 字段返回原样 list，避免封装层掺杂状态过滤逻辑
async def test_list_checkpoints_success() -> None:
    client = _FakeClient(
        {"session.checkpoints": {"checkpoints": [{"id": "c1", "status": "ready"}]}}
    )
    result = await ipc_actions.list_checkpoints(client, "sess-1")
    assert result == [{"id": "c1", "status": "ready"}]
    assert client.calls == [
        ("session.checkpoints", {"session_id": "sess-1"})
    ]


# 功能：验证 rewind 携带 checkpoint_id 回滚会话
# 设计：断言 session_id 与 checkpoint_id 都透传给 session.rewind
async def test_rewind_success() -> None:
    client = _FakeClient({"session.rewind": {"restored": ["a.txt"]}})
    result = await ipc_actions.rewind(client, "sess-1", "cp-1")
    assert result["restored"] == ["a.txt"]
    assert client.calls == [
        ("session.rewind", {"session_id": "sess-1", "checkpoint_id": "cp-1"})
    ]


# 功能：验证 get_context 返回上下文估算结果
# 设计：断言按 session_id 调用 session.context 并原样返回结果
async def test_get_context_success() -> None:
    client = _FakeClient({"session.context": {"last_run_id": "r1"}})
    result = await ipc_actions.get_context(client, "sess-1")
    assert result["last_run_id"] == "r1"
    assert client.calls == [("session.context", {"session_id": "sess-1"})]


# 功能：验证 inspect_turn 按 turn_id 加载 inspector 投影
# 设计：断言 turn_id 透传给 turn.inspect
async def test_inspect_turn_success() -> None:
    client = _FakeClient({"turn.inspect": {"turn_id": "t1"}})
    result = await ipc_actions.inspect_turn(client, "t1")
    assert result["turn_id"] == "t1"
    assert client.calls == [("turn.inspect", {"turn_id": "t1"})]


# 功能：验证 get_authority_snapshot 读取会话 authority 快照
# 设计：断言按 session_id 调用 session.get_authority 并返回 snapshot
async def test_get_authority_snapshot_success() -> None:
    client = _FakeClient(
        {"session.get_authority": {"snapshot": {"mode": "act"}}}
    )
    result = await ipc_actions.get_authority_snapshot(client, "sess-1")
    assert result["snapshot"]["mode"] == "act"
    assert client.calls == [
        ("session.get_authority", {"session_id": "sess-1"})
    ]


# 功能：验证 set_authority 全量设置 profile/mode/workspace_trust
# 设计：传入全部维度断言参数齐全，覆盖 session.set_authority 的合并语义
async def test_set_authority_all_fields() -> None:
    client = _FakeClient({"session.set_authority": {"snapshot": {}}})
    await ipc_actions.set_authority(
        client,
        "sess-1",
        profile="ask",
        mode="act",
        workspace_trust="untrusted",
    )
    assert client.calls == [
        (
            "session.set_authority",
            {
                "session_id": "sess-1",
                "profile": "ask",
                "mode": "act",
                "workspace_trust": "untrusted",
            },
        )
    ]


# 功能：验证 set_authority 只发送显式给定的字段
# 设计：验证缺省维度不会以空值混入请求参数，避免覆盖上层未指定的姿态
async def test_set_authority_only_given_fields() -> None:
    client = _FakeClient({"session.set_authority": {"snapshot": {}}})
    await ipc_actions.set_authority(client, "sess-1", profile="ask")
    assert client.calls == [
        ("session.set_authority", {"session_id": "sess-1", "profile": "ask"})
    ]


# 功能：验证 send 包装 wait_for 超时时抛出 IpcActionError("超时")
# 设计：用永久挂起的 client + 极小 timeout 强制触发超时，断言提示文案可读
async def test_send_timeout_raises_ipc_action_error() -> None:
    with pytest.raises(IpcActionError) as exc_info:
        await ipc_actions.send(
            _HangingClient(),
            "session.context",
            {"session_id": "s"},
            timeout=0.01,
        )
    assert "超时" in str(exc_info.value)


# 功能：验证 send 把 IpcError 转换（而非泄漏）为 IpcActionError
# 设计：IpcError 是 RuntimeError 子类，需要放在 RuntimeError 之前捕获，此处专门验证
async def test_send_ipc_error_raises_ipc_action_error() -> None:
    client = _ErrorClient(IpcError(-32002, "boom"))
    with pytest.raises(IpcActionError) as exc_info:
        await ipc_actions.send(
            client,
            "session.context",
            {"session_id": "s"},
        )
    assert "boom" in str(exc_info.value)


# 功能：验证 send 把普通 RuntimeError/OSError 异常转换为 IpcActionError
# 设计：未连接等本地错误也应走统一提示通道，而非让调用方处理多种异常类型
async def test_send_runtime_error_raises_ipc_action_error() -> None:
    client = _ErrorClient(RuntimeError("not connected"))
    with pytest.raises(IpcActionError) as exc_info:
        await ipc_actions.send(
            client,
            "session.context",
            {"session_id": "s"},
        )
    assert "not connected" in str(exc_info.value)


# 功能：验证 send 成功时原样返回 payload
# 设计：锁定 wait_for 的正常透传路径，不吞掉成功返回值
async def test_send_success_returns_payload() -> None:
    client = _FakeClient({"session.context": {"message_count": 5}})
    result = await ipc_actions.send(
        client,
        "session.context",
        {"session_id": "s"},
    )
    assert result["message_count"] == 5
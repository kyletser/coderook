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
    result = await ipc_actions.get_workers(client, "sess-1")
    assert result["workers"][0]["worker_id"] == "w"
    assert client.calls == [
        ("worker.list", {"limit": 50, "session_id": "sess-1"})
    ]


# 功能：Worker 控制中心封装 session 过滤、事件游标、followup 与只审查不合入协议
# 设计：用单个 fake 逐项核对 typed IPC 参数，避免 TUI 把 review 误当 apply
async def test_worker_control_center_actions() -> None:
    client = _FakeClient(
        {
            "worker.list": {"workers": []},
            "worker.events": {"events": []},
            "worker.followup": {"worker_id": "w1", "event_cursor": 2},
            "worker.review": {
                "worker_id": "w1",
                "handoff_status": "approved",
                "approved": True,
                "applied": False,
                "state_digest": "a" * 64,
            },
            "worker.apply": {
                "worker_id": "w1",
                "handoff_status": "applied",
                "changed_files": ["src/a.py"],
                "state_digest": "a" * 64,
            },
        }
    )

    await ipc_actions.get_workers(client, session_id="s1")
    await ipc_actions.start_worker(
        client,
        "s1",
        description="inspect",
        prompt="inspect repository",
        profile="reviewer",
        route_id="route-a",
        model="coder-model",
        token_budget=500,
    )
    await ipc_actions.get_worker_status(client, "s1", "w1")
    await ipc_actions.retry_worker(client, "s1", "w1")
    await ipc_actions.get_worker_events(client, "s1", "w1", after_cursor=4)
    await ipc_actions.followup_worker(client, "s1", "w1", "check tests")
    reviewed = await ipc_actions.review_worker(
        client,
        "s1",
        "w1",
        approved=True,
        confirmed=True,
        expected_digest="a" * 64,
    )
    applied = await ipc_actions.apply_worker(client, "s1", "w1", "a" * 64)

    assert reviewed["applied"] is False
    assert reviewed["state_digest"] == "a" * 64
    assert applied["handoff_status"] == "applied"
    assert applied["changed_files"] == ["src/a.py"]
    assert client.calls == [
        ("worker.list", {"limit": 50, "session_id": "s1"}),
        (
            "worker.start",
            {
                "session_id": "s1",
                "description": "inspect",
                "prompt": "inspect repository",
                "profile": "reviewer",
                "route_id": "route-a",
                "model": "coder-model",
                "read_only": True,
                "exact_files": [],
                "write_roots": [],
                "token_budget": 500,
            },
        ),
        ("worker.status", {"session_id": "s1", "worker_id": "w1"}),
        ("worker.retry", {"session_id": "s1", "worker_id": "w1"}),
        (
            "worker.events",
            {
                "session_id": "s1",
                "worker_id": "w1",
                "after_cursor": 4,
                "limit": 50,
            },
        ),
        (
            "worker.followup",
            {"session_id": "s1", "worker_id": "w1", "message": "check tests"},
        ),
        (
            "worker.review",
            {
                "session_id": "s1",
                "worker_id": "w1",
                "approved": True,
                "confirmed": True,
                "expected_digest": "a" * 64,
            },
        ),
        (
            "worker.apply",
            {
                "session_id": "s1",
                "worker_id": "w1",
                "expected_digest": "a" * 64,
                "confirmed": True,
            },
        ),
    ]


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


# 功能：验证 Change Center stage 只发送显式文件集合、会话和确认位
# 设计：用记录型 fake client 检查 typed IPC 参数，防止 TUI 退回隐式 stage-all
async def test_stage_changes_sends_selected_paths_and_confirmation() -> None:
    client = _FakeClient({"workspace.stage": {"payload": {"files": []}}})

    await ipc_actions.stage_changes(
        client,
        "sess-1",
        ["src/app.py", "tests/test_app.py"],
        "a" * 64,
    )

    assert client.calls == [
        (
            "workspace.stage",
            {
                "session_id": "sess-1",
                "paths": ["src/app.py", "tests/test_app.py"],
                "expected_digest": "a" * 64,
                "confirmed": True,
            },
        )
    ]


# 功能：验证 Change Center commit 只创建本地提交并发送显式确认位
# 设计：固定会话和主题检查 typed IPC，不在客户端拼接 Git 命令或 push 行为
async def test_commit_changes_sends_subject_and_confirmation() -> None:
    client = _FakeClient({"workspace.commit": {"commit": "abc123", "files": []}})

    result = await ipc_actions.commit_changes(
        client,
        "sess-1",
        "fix: verified",
        "b" * 64,
    )

    assert result["commit"] == "abc123"
    assert client.calls == [
        (
            "workspace.commit",
            {
                "session_id": "sess-1",
                "message": "fix: verified",
                "expected_digest": "b" * 64,
                "confirmed": True,
            },
        )
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


# 功能：验证 rewind 预览与执行分别使用只读摘要和显式确认协议
# 设计：先请求预览再携带摘要恢复，固定二阶段 typed IPC 合同
async def test_rewind_success() -> None:
    client = _FakeClient(
        {
            "session.rewind_preview": {"state_digest": "a" * 64},
            "session.rewind": {"restored": ["a.txt"]},
        }
    )
    preview = await ipc_actions.preview_rewind(client, "sess-1", "cp-1")
    result = await ipc_actions.rewind(client, "sess-1", "cp-1", "a" * 64)
    assert preview["state_digest"] == "a" * 64
    assert result["restored"] == ["a.txt"]
    assert client.calls == [
        (
            "session.rewind_preview",
            {"session_id": "sess-1", "checkpoint_id": "cp-1"},
        ),
        (
            "session.rewind",
            {
                "session_id": "sess-1",
                "checkpoint_id": "cp-1",
                "expected_digest": "a" * 64,
                "confirmed": True,
            },
        ),
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


# 功能：验证 list_mcp_servers 调用 mcp.list 并原样返回
# 设计：断言方法名与空参数，返回值即 daemon 组装好的 server 清单
async def test_list_mcp_servers_success() -> None:
    client = _FakeClient({"mcp.list": {"servers": [{"name": "github"}]}})
    result = await ipc_actions.list_mcp_servers(client)
    assert result["servers"][0]["name"] == "github"
    assert client.calls == [("mcp.list", {})]


# 功能：验证 list_hooks 携带 limit 调用 hooks.list
# 设计：默认 20 的 limit 应透传，返回原始 payload dict
async def test_list_hooks_success() -> None:
    client = _FakeClient({"hooks.list": {"configs": []}})
    result = await ipc_actions.list_hooks(client)
    assert result["configs"] == []
    assert client.calls == [("hooks.list", {"limit": 20})]


# 功能：验证 rerun_hook 按 hook_id 调用 hooks.rerun
# 设计：断言 hook_id 透传且返回值即新产生的审计记录
async def test_rerun_hook_success() -> None:
    client = _FakeClient({"hooks.rerun": {"hook_id": "h1"}})
    result = await ipc_actions.rerun_hook(client, "h1")
    assert result["hook_id"] == "h1"
    assert client.calls == [("hooks.rerun", {"hook_id": "h1"})]


# 功能：验证 list_memories 调用 memory.list
# 设计：断言方法名与空参数，返回原始 payload
async def test_list_memories_success() -> None:
    client = _FakeClient({"memory.list": {"memories": []}})
    result = await ipc_actions.list_memories(client)
    assert result["memories"] == []
    assert client.calls == [("memory.list", {})]


# 功能：验证新增和编辑记忆使用独立 typed IPC 且保留会话来源
# 设计：连续调用两个 action 并精确断言 payload，避免 TUI 回退到自由格式 Agent 提示
async def test_add_and_edit_memory_use_typed_ipc() -> None:
    client = _FakeClient(
        {
            "memory.add": {"memory": {"id": "m1"}},
            "memory.edit": {"memory": {"id": "m1"}},
        }
    )

    await ipc_actions.add_memory(
        client,
        name="tests",
        body="Run pytest.",
        source_session_id="sess-1",
    )
    await ipc_actions.edit_memory(client, "m1", body="Run uv run pytest.")

    assert client.calls == [
        (
            "memory.add",
            {
                "name": "tests",
                "body": "Run pytest.",
                "description": "",
                "memory_type": "project",
                "source_session_id": "sess-1",
            },
        ),
        ("memory.edit", {"memory_id": "m1", "body": "Run uv run pytest."}),
    ]


# 功能：验证 pin、expire 与自动保存设置通过 typed IPC 精确传递
# 设计：覆盖 bool、nullable 时间和枚举三类参数，防止字符串拼接造成语义漂移
async def test_memory_governance_actions_use_typed_ipc() -> None:
    client = _FakeClient(
        {
            "memory.pin": {"memory": {"id": "m1"}},
            "memory.expire": {"memory": {"id": "m1"}},
            "memory.settings.set": {"settings": {"auto_save": "off"}},
        }
    )

    await ipc_actions.pin_memory(client, "m1", pinned=False)
    await ipc_actions.expire_memory(client, "m1", expires_at=None)
    await ipc_actions.set_memory_auto_save(client, "off")

    assert client.calls == [
        ("memory.pin", {"memory_id": "m1", "pinned": False}),
        ("memory.expire", {"memory_id": "m1", "expires_at": None}),
        ("memory.settings.set", {"auto_save": "off"}),
    ]


# 功能：验证 delete_memory 按 memory_id 调用 memory.delete
# 设计：断言 memory_id 透传
async def test_delete_memory_success() -> None:
    client = _FakeClient({"memory.delete": {"deleted": "m1"}})
    result = await ipc_actions.delete_memory(client, "m1")
    assert result["deleted"] == "m1"
    assert client.calls == [("memory.delete", {"memory_id": "m1"})]


# 功能：验证 get_background 缺省 job_id 时查询任务列表
# 设计：job_id 为空时参数固定为 {}，覆盖列表模式的调用约定
async def test_get_background_list_default() -> None:
    client = _FakeClient({"background.get": {"jobs": []}})
    result = await ipc_actions.get_background(client, "sess-1")
    assert result["jobs"] == []
    assert client.calls == [
        ("background.get", {"session_id": "sess-1", "job_id": ""})
    ]


# 功能：验证 get_background 带 job_id 时查询单个任务输出
# 设计：job_id 非空时透传，覆盖查看增量输出的调用约定
async def test_get_background_by_job() -> None:
    client = _FakeClient({"background.get": {"jobs": [{"id": "j1"}]}})
    result = await ipc_actions.get_background(client, "sess-1", job_id="j1")
    assert result["jobs"][0]["id"] == "j1"
    assert client.calls == [
        ("background.get", {"session_id": "sess-1", "job_id": "j1"})
    ]


# 功能：验证 cancel_background 按 job_id 调用 background.cancel
# 设计：断言 job_id 透传
async def test_cancel_background_success() -> None:
    client = _FakeClient({"background.cancel": {"job_id": "j1"}})
    result = await ipc_actions.cancel_background(client, "sess-1", "j1")
    assert result["job_id"] == "j1"
    assert client.calls == [
        ("background.cancel", {"session_id": "sess-1", "job_id": "j1"})
    ]


# 功能：验证 cancel_worker 按 session_id 与 worker_id 调用 worker.cancel
# 设计：断言会话边界和 Worker 标识同时透传，避免跨会话取消
async def test_cancel_worker_success() -> None:
    client = _FakeClient({"worker.cancel": {"worker_id": "w1"}})
    result = await ipc_actions.cancel_worker(client, "s1", "w1")
    assert result["worker_id"] == "w1"
    assert client.calls == [
        ("worker.cancel", {"session_id": "s1", "worker_id": "w1"})
    ]


# 功能：验证计划审阅决定通过 typed plan.respond 发送完整归属与修订字段
# 设计：用记录型 fake 精确核对 session/run/decision，防止客户端退回本地墓碑状态
async def test_respond_plan_uses_typed_durable_command() -> None:
    client = _FakeClient({"plan.respond": {"status": "resolved"}})

    result = await ipc_actions.respond_plan(
        client,
        "sess-plan",
        "run-plan",
        "revise",
        revision="check tests",
    )

    assert result["status"] == "resolved"
    assert client.calls == [
        (
            "plan.respond",
            {
                "session_id": "sess-plan",
                "run_id": "run-plan",
                "decision": "revise",
                "revision": "check tests",
            },
        )
    ]

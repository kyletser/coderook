from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_rook.core.app import CoreApp
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.fleet import LocalFleet, LocalFleetScheduler, LocalWorkerRequest
from code_rook.core.subagent import BackgroundTaskRegistry
from code_rook.core.workflow import WorkerExecutionResult, WorkflowLedger


class _Host:
    # 初始化无网络的 workflow IPC 测试 host
    def __init__(self) -> None:
        self.calls: list[str] = []

    # 返回可持久化的最小成功 Worker 结果
    async def run(self, request: LocalWorkerRequest) -> WorkerExecutionResult:
        self.calls.append(request.step.id)
        return WorkerExecutionResult(
            status="completed",
            summary="done",
            evidence=["unit:passed"],
        )


# 构造只含一个 worker 的声明式 workflow JSON
def _source() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "id": "ipc-workflow",
            "name": "ipc workflow",
            "root": {
                "type": "worker",
                "id": "build",
                "description": "build",
                "prompt": "build",
            },
        }
    )


# 功能：workflow.start/list/get IPC handler 查询同一个 SQLite reducer 状态
# 设计：后台启动最小 workflow，等待终态后分别调用 list/get，避免以内存 task 作为断言来源
@pytest.mark.asyncio
async def test_workflow_ipc_handlers_share_durable_graph(tmp_path: Path) -> None:
    host = _Host()
    scheduler = LocalFleetScheduler(
        BackgroundTaskRegistry(),
        host,
        workspace=tmp_path,
    )
    fleet = LocalFleet(WorkflowLedger(tmp_path / "workflow.db"), scheduler)
    app = CoreApp()
    app._labs_enabled = True
    app._fleet = fleet

    started = await app._workflow_start_handler(
        {"source": _source(), "format": "json"}
    )
    for _ in range(100):
        graph = fleet.graph(started.workflow_id)
        if graph.status in {"completed", "failed"}:
            break
        await asyncio.sleep(0.01)
    listed = await app._workflow_list_handler({"limit": 10})
    fetched = await app._workflow_get_handler({"workflow_id": started.workflow_id})
    await fleet.shutdown()

    assert started.status == "started"
    assert host.calls == ["build"]
    assert listed.workflows[0]["status"] == "completed"
    assert fetched.workflow["status"] == "completed"
    assert fetched.workflow["nodes"]["build"]["evidence"] == ["unit:passed"]


# 功能：workflow.get 对未知 durable ID 返回 typed INVALID_PARAMS 错误
# 设计：使用空 ledger 调用真实 handler，证明 TUI 不会收到伪造的 pending graph
@pytest.mark.asyncio
async def test_workflow_get_rejects_unknown_id(tmp_path: Path) -> None:
    fleet = LocalFleet(
        WorkflowLedger(tmp_path / "workflow.db"),
        LocalFleetScheduler(
            BackgroundTaskRegistry(),
            _Host(),
            workspace=tmp_path,
        ),
    )
    app = CoreApp()
    app._labs_enabled = True
    app._fleet = fleet

    with pytest.raises(HandlerError, match="workflow not found"):
        await app._workflow_get_handler({"workflow_id": "missing"})

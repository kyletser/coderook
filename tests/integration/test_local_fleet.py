from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from code_rook.core.fleet import (
    FleetProfile,
    LocalFleet,
    LocalFleetScheduler,
    LocalProcessHost,
    LocalWorkerRequest,
    SQLiteWorkerStore,
)
from code_rook.core.subagent import BackgroundTaskRegistry, WorkerConflictError
from code_rook.core.subagent.models import WorkerStatus
from code_rook.core.workflow import (
    WorkerExecutionResult,
    WorkerStep,
    WorkflowLedger,
    WorkflowSpec,
    parse_workflow_text,
)


class _ControlledHost:
    # 初始化可阻塞、可 crash 的确定性 Fleet host
    def __init__(self, *, cancel_once: set[str] | None = None) -> None:
        self.cancel_once = cancel_once or set()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.calls: list[str] = []

    # 记录请求并按测试控制信号返回或模拟进程中断
    async def run(self, request: LocalWorkerRequest) -> WorkerExecutionResult:
        step_id = request.step.id
        self.calls.append(step_id)
        self.started.set()
        if step_id in self.cancel_once:
            self.cancel_once.remove(step_id)
            raise asyncio.CancelledError
        if self.block:
            await self.release.wait()
        return WorkerExecutionResult(
            status="completed",
            summary=f"completed {step_id}",
            evidence=[f"evidence:{step_id}"],
            receipt={"attempt": request.attempt, "worker": step_id},
        )


# 从 JSON 字典生成严格 WorkflowSpec
def _spec(workflow_id: str, root: dict[str, object]) -> WorkflowSpec:
    return parse_workflow_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": workflow_id,
                "name": workflow_id,
                "root": root,
            }
        ),
        format="json",
    )


# 构造带固定 profile 的本地 Fleet scheduler
def _scheduler(
    path: Path,
    host: _ControlledHost,
    workspace: Path,
    *,
    boot_id: str,
) -> tuple[LocalFleetScheduler, BackgroundTaskRegistry]:
    registry = BackgroundTaskRegistry(
        store=SQLiteWorkerStore(path),
        boot_id=boot_id,
    )
    scheduler = LocalFleetScheduler(
        registry,
        host,
        workspace=workspace,
        profiles=[
            FleetProfile(
                name="builder",
                route="route-a",
                model="model-a",
                reasoning="high",
            )
        ],
        default_profile="builder",
        heartbeat_interval_s=0.01,
        lease_timeout_s=0.1,
    )
    return scheduler, registry


# 功能：固定 LocalProcessHost 通过 JSON 协议执行真实本地进程并持久 profile receipt
# 设计：使用仓库内无网络 fixture 进程，验证 argv 不来自 IR 且 SQLite 可从新实例离线读取
@pytest.mark.asyncio
async def test_local_process_worker_uses_fixed_profile_and_sqlite(
    tmp_path: Path,
) -> None:
    worker_script = Path(__file__).parents[1] / "fixtures" / "fleet_worker.py"
    host = LocalProcessHost(
        (sys.executable, str(worker_script)),
        cwd=tmp_path,
    )
    store = SQLiteWorkerStore(tmp_path / "fleet.db")
    registry = BackgroundTaskRegistry(store=store, boot_id="boot-a")
    scheduler = LocalFleetScheduler(
        registry,
        host,
        workspace=tmp_path,
        profiles=[
            FleetProfile(
                name="builder",
                route="route-a",
                model="model-a",
                reasoning="high",
            )
        ],
        default_profile="builder",
        heartbeat_interval_s=0.01,
        lease_timeout_s=0.1,
    )
    spec = _spec(
        "local-process",
        {
            "type": "worker",
            "id": "build",
            "description": "build",
            "prompt": "build release",
        },
    )

    graph = await LocalFleet(
        WorkflowLedger(tmp_path / "workflow.db"), scheduler
    ).run(spec)
    restored = SQLiteWorkerStore(tmp_path / "fleet.db").get(
        "workflow:local-process:build"
    )

    assert graph.status == "completed"
    assert restored.status == WorkerStatus.COMPLETED
    assert restored.route == "route-a"
    assert restored.model == "model-a"
    assert restored.reasoning == "high"
    assert restored.token_usage == 7
    assert restored.receipt["route"] == "route-a"


# 功能：daemon 重启恢复 SQLite Fleet 与 workflow 时不重复已完成节点
# 设计：第二个节点首次抛 CancelledError，换 boot/registry 后恢复同一双 ledger 并统计调用
@pytest.mark.asyncio
async def test_fleet_restart_resumes_without_repeating_completed_node(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "workflow.db"
    fleet_path = tmp_path / "fleet.db"
    spec = _spec(
        "fleet-resume",
        {
            "type": "sequence",
            "id": "root",
            "steps": [
                {
                    "type": "worker",
                    "id": "first",
                    "description": "first",
                    "prompt": "first",
                },
                {
                    "type": "worker",
                    "id": "second",
                    "description": "second",
                    "prompt": "second",
                },
            ],
        },
    )
    first_host = _ControlledHost(cancel_once={"second"})
    first_scheduler, _ = _scheduler(
        fleet_path, first_host, tmp_path, boot_id="boot-a"
    )

    with pytest.raises(asyncio.CancelledError):
        await LocalFleet(WorkflowLedger(workflow_path), first_scheduler).run(spec)

    second_host = _ControlledHost()
    second_scheduler, second_registry = _scheduler(
        fleet_path, second_host, tmp_path, boot_id="boot-b"
    )
    graph = await LocalFleet(
        WorkflowLedger(workflow_path), second_scheduler
    ).run(spec)

    assert graph.status == "completed"
    assert first_host.calls == ["first", "second"]
    assert second_host.calls == ["second"]
    assert second_registry.record("workflow:fleet-resume:first").attempt == 1  # type: ignore[union-attr]
    assert second_registry.record("workflow:fleet-resume:second").attempt == 2  # type: ignore[union-attr]


# 功能：Fleet 在启动第二个本地进程前拒绝跨 workflow 的重叠 write claim
# 设计：让首个 host 持续运行保持 active claim，再直接调度同文件写入并断言 fail closed
@pytest.mark.asyncio
async def test_fleet_parallel_write_claim_conflict_fails_before_host(
    tmp_path: Path,
) -> None:
    host = _ControlledHost()
    host.block = True
    scheduler, _ = _scheduler(tmp_path / "fleet.db", host, tmp_path, boot_id="boot-a")
    claim = {"read_only": False, "exact_files": ["release.toml"]}
    first = WorkerStep(
        id="first",
        description="first",
        prompt="first",
        write_claim=claim,
    )
    second = WorkerStep(
        id="second",
        description="second",
        prompt="second",
        write_claim=claim,
    )
    first_task = asyncio.create_task(
        scheduler.run_worker("workflow-a", first, attempt=1)
    )
    await host.started.wait()

    with pytest.raises(WorkerConflictError, match="write claim conflicts"):
        await scheduler.run_worker("workflow-b", second, attempt=1)
    host.release.set()
    await first_task

    assert host.calls == ["first"]


# 功能：运行中的本地 Fleet Worker 按 lease 配置持续写入 heartbeat
# 设计：阻塞 host 超过两个 heartbeat 周期并比较 SQLite 中的时间字段，再释放进程
@pytest.mark.asyncio
async def test_fleet_refreshes_heartbeat_while_process_runs(tmp_path: Path) -> None:
    host = _ControlledHost()
    host.block = True
    scheduler, registry = _scheduler(
        tmp_path / "fleet.db", host, tmp_path, boot_id="boot-a"
    )
    step = WorkerStep(id="slow", description="slow", prompt="slow")
    task = asyncio.create_task(scheduler.run_worker("heartbeat", step, attempt=1))
    await host.started.wait()
    worker_id = "workflow:heartbeat:slow"
    before = registry.record(worker_id)
    assert before is not None
    await asyncio.sleep(0.03)
    after = registry.record(worker_id)
    assert after is not None
    host.release.set()
    await task

    assert after.heartbeat_at > before.heartbeat_at


# 功能：节点不能覆盖 FleetProfile 固定的 route/model/reasoning 配置
# 设计：在 host 启动前提交冲突 model，断言无任何本地进程请求产生
@pytest.mark.asyncio
async def test_fleet_profile_override_is_rejected(tmp_path: Path) -> None:
    host = _ControlledHost()
    scheduler, _ = _scheduler(tmp_path / "fleet.db", host, tmp_path, boot_id="boot-a")
    step = WorkerStep(
        id="override",
        description="override",
        prompt="override",
        model="other-model",
    )

    with pytest.raises(ValueError, match="overrides fixed profile model"):
        await scheduler.run_worker("profile", step, attempt=1)

    assert host.calls == []

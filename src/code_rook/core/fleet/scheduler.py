from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.fleet.models import FleetProfile, LocalWorkerRequest
from code_rook.core.subagent.models import (
    ACTIVE_WORKER_STATUSES,
    WorkerRecord,
    WorkerStatus,
)
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.workflow import (
    WorkerExecutionResult,
    WorkerStep,
    WorkflowExecutor,
    WorkflowLedger,
    WorkflowSpec,
    WorkGraphState,
)


class FleetHostAdapter(Protocol):
    # 在受信任 host 上执行一个固定协议的本地 Worker 请求
    async def run(self, request: LocalWorkerRequest) -> WorkerExecutionResult: ...


class LocalFleetScheduler:
    # 绑定本地 host、durable Worker registry 和不可变 profile 配置
    def __init__(
        self,
        registry: BackgroundTaskRegistry,
        host: FleetHostAdapter,
        *,
        workspace: Path,
        profiles: list[FleetProfile] | None = None,
        default_profile: str = "default",
        max_concurrency: int = 4,
        heartbeat_interval_s: float = 10.0,
        lease_timeout_s: float = 30.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("fleet max_concurrency must be positive")
        configured = profiles or [FleetProfile(name="default")]
        self._profiles = {profile.name: profile for profile in configured}
        if len(self._profiles) != len(configured):
            raise ValueError("fleet profile names must be unique")
        if default_profile not in self._profiles:
            raise ValueError(f"default fleet profile is not configured: {default_profile}")
        self._registry = registry
        self._host = host
        self._workspace = workspace.resolve()
        self._default_profile = default_profile
        self._slots = asyncio.Semaphore(max_concurrency)
        self._heartbeat_interval_s = heartbeat_interval_s
        self._lease_timeout_s = lease_timeout_s

    # 解析 profile 并拒绝节点级 route/model/reasoning/authority 漂移
    def _resolve_step(self, step: WorkerStep) -> WorkerStep:
        profile_name = step.profile or self._default_profile
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise ValueError(f"fleet profile is not configured: {profile_name}")
        fixed_fields = {
            "route": profile.route,
            "model": profile.model,
            "reasoning": profile.reasoning,
        }
        for field_name, fixed_value in fixed_fields.items():
            requested = str(getattr(step, field_name))
            if requested and requested != fixed_value:
                raise ValueError(
                    f"worker {step.id} overrides fixed profile {field_name}"
                )
        default_authority = AuthoritySnapshot()
        if (
            step.authority_ceiling != default_authority
            and step.authority_ceiling != profile.authority_ceiling
        ):
            raise ValueError(
                f"worker {step.id} overrides fixed profile authority ceiling"
            )
        return step.model_copy(
            update={
                "profile": profile.name,
                "route": profile.route,
                "model": profile.model,
                "reasoning": profile.reasoning,
                "authority_ceiling": profile.authority_ceiling,
            }
        )

    # 将 workflow/node ID 映射为稳定的 Fleet Worker ID
    @staticmethod
    def _worker_id(workflow_id: str, step_id: str) -> str:
        return f"workflow:{workflow_id}:{step_id}"

    # 从 durable WorkerRecord 重建执行结果，避免 crash 窗口重复运行已完成进程
    @staticmethod
    def _restored_result(worker: WorkerRecord) -> WorkerExecutionResult:
        status = "completed" if worker.status == WorkerStatus.COMPLETED else "failed"
        if worker.status == WorkerStatus.BUDGET_LIMITED:
            status = "budget_limited"
        return WorkerExecutionResult(
            status=status,
            summary=worker.summary or worker.status_reason,
            evidence=worker.evidence,
            artifact_handles=worker.artifact_handles,
            token_usage=0,
            approved=worker.approved,
            receipt=worker.receipt,
        )

    # 定期刷新外部本地进程 Worker 的 lease heartbeat
    async def _heartbeat_loop(self, worker_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            worker = self._registry.record(worker_id)
            if worker is None or worker.status not in ACTIVE_WORKER_STATUSES:
                return
            self._registry.heartbeat(worker_id)

    # 把声明式 WorkerStep 调度为可恢复的本地进程 Worker
    async def run_worker(
        self,
        workflow_id: str,
        step: WorkerStep,
        *,
        attempt: int,
    ) -> WorkerExecutionResult:
        del attempt
        resolved = self._resolve_step(step)
        worker_id = self._worker_id(workflow_id, resolved.id)
        worker = self._registry.record(worker_id)
        if worker is not None and worker.status in {
            WorkerStatus.COMPLETED,
            WorkerStatus.BUDGET_LIMITED,
        }:
            return self._restored_result(worker)
        if worker is not None and worker.status in {
            WorkerStatus.FAILED,
            WorkerStatus.INTERRUPTED,
        }:
            worker = self._registry.prepare_retry(
                worker_id,
                authority_ceiling=resolved.authority_ceiling,
            )
        elif worker is not None:
            raise ValueError(
                f"fleet worker {worker_id} is already {worker.status.value}"
            )
        else:
            worker = self._registry.new_record(
                worker_id=worker_id,
                parent_turn_id=f"workflow:{workflow_id}",
                root_goal_id=workflow_id,
                description=resolved.description,
                prompt=resolved.prompt,
                workspace=str(self._workspace),
                authority_ceiling=resolved.authority_ceiling,
                depth=1,
                max_steps=20,
                role="workflow-worker",
                profile=resolved.profile,
                route=resolved.route,
                model=resolved.model,
                reasoning=resolved.reasoning,
                write_claim=resolved.write_claim,
                acceptance=resolved.acceptance,
                wall_time_s=resolved.wall_time_s,
                heartbeat_interval_s=self._heartbeat_interval_s,
                lease_timeout_s=self._lease_timeout_s,
                max_attempts=10,
                retry_backoff_s=0,
            )
            self._registry.create(worker)
        worker = self._registry.start(worker.id)
        self._registry.append_event(worker.id, "worker.started", resolved.description)
        heartbeat = asyncio.create_task(self._heartbeat_loop(worker.id))
        try:
            request = LocalWorkerRequest(
                workflow_id=workflow_id,
                worker_id=worker.id,
                workspace=str(self._workspace),
                attempt=worker.attempt,
                step=resolved,
            )
            async with self._slots:
                result = await self._host.run(request)
        except asyncio.CancelledError:
            self._registry.update_status(
                worker.id,
                WorkerStatus.INTERRUPTED,
                reason="fleet scheduler interrupted",
            )
            raise
        except Exception as exc:
            result = WorkerExecutionResult(
                status="failed",
                summary=f"local host failed: {type(exc).__name__}: {exc}"[:4_000],
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        self._registry.add_token_usage(worker.id, result.token_usage)
        status = {
            "completed": WorkerStatus.COMPLETED,
            "failed": WorkerStatus.FAILED,
            "budget_limited": WorkerStatus.BUDGET_LIMITED,
        }[result.status]
        self._registry.update_status(
            worker.id,
            status,
            reason="" if status == WorkerStatus.COMPLETED else result.summary,
            summary=result.summary,
            evidence=result.evidence,
            artifact_handles=result.artifact_handles,
            approved=result.approved,
            receipt=result.receipt,
        )
        self._registry.append_event(
            worker.id,
            f"worker.{status.value}",
            result.summary or status.value,
        )
        return result


class LocalFleet:
    # 组装 WorkflowExecutor 与 LocalFleetScheduler，并管理 daemon 级后台恢复任务
    def __init__(
        self,
        ledger: WorkflowLedger,
        scheduler: LocalFleetScheduler,
    ) -> None:
        self._ledger = ledger
        self._scheduler = scheduler
        self._tasks: dict[str, asyncio.Task[WorkGraphState]] = {}

    # 在当前协程执行或恢复一个 workflow
    async def run(self, spec: WorkflowSpec) -> WorkGraphState:
        return await WorkflowExecutor(self._ledger, self._scheduler).run(spec)

    # 后台启动一个 workflow 并返回稳定 ID
    def start(self, spec: WorkflowSpec) -> str:
        self._ledger.create(spec)
        existing = self._tasks.get(spec.id)
        if existing is not None and not existing.done():
            raise ValueError(f"workflow is already running: {spec.id}")
        task = asyncio.create_task(self.run(spec), name=f"workflow:{spec.id}")
        self._tasks[spec.id] = task

        # 完成后仅移除内存句柄，durable ledger 保留全部状态
        def discard(completed: asyncio.Task[WorkGraphState]) -> None:
            if self._tasks.get(spec.id) is completed:
                self._tasks.pop(spec.id, None)

        task.add_done_callback(discard)
        return spec.id

    # 恢复 ledger 中 running/interrupted workflow，completed 节点由执行器直接复用
    def resume_all(self) -> list[str]:
        resumed: list[str] = []
        for item in self._ledger.list():
            if item["status"] not in {"running", "interrupted"}:
                continue
            spec = self._ledger.get_spec(item["id"])
            self.start(spec)
            resumed.append(spec.id)
        return resumed

    # 查询 durable workflow graph，不读取私有 executor 内存
    def graph(self, workflow_id: str) -> WorkGraphState:
        return self._ledger.graph(workflow_id)

    # 列出 durable workflow 元数据
    def list(self) -> list[dict[str, str]]:
        return self._ledger.list()

    # 取消本 daemon 的 workflow tasks，使进程 Worker 进入 interrupted 供下次恢复
    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

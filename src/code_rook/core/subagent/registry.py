from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.context import ExecutionContext
from code_rook.core.subagent.models import (
    ACTIVE_WORKER_STATUSES,
    WorkerEvent,
    WorkerRecord,
    WorkerStatus,
    WriteClaim,
)
from code_rook.core.subagent.store import WorkerStore, WorkerStoreError


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将路径转换为可稳定比较的绝对大小写归一形式
def _normalized_path(value: str, workspace: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = Path(workspace) / path
    return Path(os.path.normcase(str(path.resolve(strict=False))))


# 判断一个路径是否等于或位于另一个目录之下
def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass
class _BackgroundEntry:
    task: asyncio.Task[None]
    context: ExecutionContext
    parent_run_id: str


class WorkerConflictError(ValueError):
    pass


class WorkerBudgetError(ValueError):
    pass


# 管理持久 WorkerRecord 与当前 daemon 内 asyncio 任务句柄
class BackgroundTaskRegistry:
    # 初始化 Worker 控制面，并把上一 daemon 遗留的活跃 Worker 标记为 interrupted
    def __init__(
        self,
        max_entries: int = 256,
        *,
        store_path: Path | None = None,
        boot_id: str | None = None,
        recover: bool = True,
    ) -> None:
        self._tasks: dict[str, _BackgroundEntry] = {}
        self._memory_records: dict[str, WorkerRecord] = {}
        self._max_entries = max(1, max_entries)
        self._store = WorkerStore(store_path) if store_path is not None else None
        self.boot_id = boot_id or uuid.uuid4().hex
        self._shutting_down = False
        if recover:
            self.recover_stale()

    # 进入 daemon shutdown 模式，使后续任务取消记录为可恢复 interrupted
    def begin_shutdown(self) -> None:
        self._shutting_down = True

    # 构造带当前 boot 和时间戳的初始 WorkerRecord
    def new_record(
        self,
        *,
        worker_id: str,
        parent_turn_id: str,
        root_goal_id: str,
        description: str,
        prompt: str,
        workspace: str,
        authority_ceiling: AuthoritySnapshot,
        depth: int,
        max_steps: int,
        session_id: str = "",
        parent_worker_id: str = "",
        role: str = "general-purpose",
        profile: str = "",
        route: str = "",
        model: str = "",
        worktree: str = "",
        branch: str = "",
        merge_owner: str = "",
        merge_reviewer: str = "",
        write_claim: WriteClaim | None = None,
        dependencies: list[str] | None = None,
        acceptance: list[str] | None = None,
        wall_time_s: int = 900,
        heartbeat_interval_s: float = 10.0,
        lease_timeout_s: float = 30.0,
        token_budget: int | None = None,
        max_attempts: int = 3,
        retry_backoff_s: float = 1.0,
    ) -> WorkerRecord:
        now = _now()
        return WorkerRecord(
            id=worker_id,
            parent_turn_id=parent_turn_id,
            parent_worker_id=parent_worker_id,
            root_goal_id=root_goal_id,
            session_id=session_id,
            description=description,
            prompt=prompt,
            role=role,
            profile=profile,
            route=route,
            model=model,
            depth=depth,
            max_steps=max_steps,
            wall_time_s=wall_time_s,
            workspace=workspace,
            worktree=worktree,
            branch=branch,
            merge_owner=merge_owner,
            merge_reviewer=merge_reviewer,
            authority_ceiling=authority_ceiling,
            write_claim=write_claim or WriteClaim(read_only=True),
            dependencies=list(dependencies or []),
            acceptance=list(acceptance or []),
            heartbeat_at=now,
            heartbeat_interval_s=heartbeat_interval_s,
            lease_timeout_s=lease_timeout_s,
            boot_id=self.boot_id,
            token_budget=token_budget,
            max_attempts=max_attempts,
            retry_backoff_s=retry_backoff_s,
            created_at=now,
            updated_at=now,
        )

    # 从磁盘或内存读取指定 WorkerRecord
    def record(self, worker_id: str) -> WorkerRecord | None:
        if self._store is not None:
            try:
                return self._store.get(worker_id)
            except WorkerStoreError:
                return None
        return self._memory_records.get(worker_id)

    # 稳定列出全部 WorkerRecord
    def list_records(self) -> list[WorkerRecord]:
        if self._store is not None:
            return self._store.list()
        return sorted(
            self._memory_records.values(),
            key=lambda item: (item.created_at, item.id),
        )

    # 保存 WorkerRecord 到当前 registry 的持久或内存 backend
    def _save(self, worker: WorkerRecord) -> None:
        if self._store is not None:
            self._store.save(worker)
        else:
            self._memory_records[worker.id] = worker

    # 判断两个声明是否处于同一可冲突工作空间
    @staticmethod
    def _same_claim_domain(left: WorkerRecord, right: WorkerRecord) -> bool:
        return _normalized_path(left.workspace, left.workspace) == _normalized_path(
            right.workspace, right.workspace
        )

    # 判断两个写入声明的文件或目录范围是否相交
    @staticmethod
    def _claims_overlap(left: WorkerRecord, right: WorkerRecord) -> bool:
        left_claim = left.write_claim
        right_claim = right.write_claim
        if left_claim.read_only or right_claim.read_only:
            return False
        if (
            left_claim.coordination_contract.strip()
            and left_claim.coordination_contract.strip()
            == right_claim.coordination_contract.strip()
        ):
            return False
        left_files = {
            _normalized_path(path, left.workspace) for path in left_claim.exact_files
        }
        right_files = {
            _normalized_path(path, right.workspace) for path in right_claim.exact_files
        }
        left_roots = [
            _normalized_path(path, left.workspace) for path in left_claim.write_roots
        ]
        right_roots = [
            _normalized_path(path, right.workspace) for path in right_claim.write_roots
        ]
        if left_files & right_files:
            return True
        if any(_within(path, root) for path in left_files for root in right_roots):
            return True
        if any(_within(path, root) for path in right_files for root in left_roots):
            return True
        return any(
            _within(left_root, right_root) or _within(right_root, left_root)
            for left_root in left_roots
            for right_root in right_roots
        )

    # 在启动前拒绝同一 workspace 内仍活跃的相交写入声明
    def validate_claim(self, candidate: WorkerRecord) -> None:
        for existing in self.list_records():
            if existing.id == candidate.id or existing.status not in ACTIVE_WORKER_STATUSES:
                continue
            if self._same_claim_domain(existing, candidate) and self._claims_overlap(
                existing, candidate
            ):
                raise WorkerConflictError(
                    f"write claim conflicts with active worker {existing.id}"
                )

    # 保存 queued WorkerRecord，使 claim 冲突在创建 asyncio.Task 前失败
    def create(self, worker: WorkerRecord) -> WorkerRecord:
        if self.record(worker.id) is not None:
            raise WorkerConflictError(f"worker already exists: {worker.id}")
        same_goal = [
            item
            for item in self.list_records()
            if item.root_goal_id == worker.root_goal_id
        ]
        known_budgets = {
            item.token_budget for item in same_goal if item.token_budget is not None
        }
        if worker.token_budget is not None:
            known_budgets.add(worker.token_budget)
        if len(known_budgets) > 1:
            raise WorkerConflictError("root goal workers must share one token budget")
        shared_budget = next(iter(known_budgets), None)
        if shared_budget is not None:
            worker.token_budget = shared_budget
            for sibling in same_goal:
                if sibling.token_budget is None:
                    sibling.token_budget = shared_budget
                    sibling.updated_at = _now()
                    self._save(sibling)
        for dependency_id in worker.dependencies:
            dependency = self.record(dependency_id)
            if dependency is None or dependency.status != WorkerStatus.COMPLETED:
                raise WorkerConflictError(
                    f"worker dependency is not completed: {dependency_id}"
                )
        self.validate_claim(worker)
        self._save(worker)
        return worker

    # 注册当前进程任务句柄，并将已创建的 WorkerRecord 转为 running
    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
        parent_run_id: str = "",
    ) -> None:
        worker = self.record(run_id)
        if worker is None:
            worker = self.new_record(
                worker_id=run_id,
                parent_turn_id=parent_run_id or run_id,
                root_goal_id=parent_run_id or run_id,
                description=context.goal[:80] or "background worker",
                prompt=context.goal,
                workspace=str(Path.cwd()),
                authority_ceiling=AuthoritySnapshot(),
                depth=1,
                max_steps=context.max_steps,
            )
            self.create(worker)
        now = _now()
        worker.status = WorkerStatus.RUNNING
        worker.status_reason = ""
        worker.started_at = worker.started_at or now
        worker.heartbeat_at = now
        worker.updated_at = now
        worker.boot_id = self.boot_id
        self._save(worker)
        self._tasks[run_id] = _BackgroundEntry(task, context, parent_run_id)
        task.add_done_callback(lambda _task: self._prune_completed())

    # 超过容量时按注册顺序淘汰最早的已完成内存句柄，持久记录不删除
    def _prune_completed(self) -> None:
        for run_id, entry in list(self._tasks.items()):
            if len(self._tasks) <= self._max_entries:
                break
            if entry.task.done():
                self._tasks.pop(run_id, None)

    # 查询当前进程内后台任务及其上下文
    def get(self, run_id: str) -> tuple[asyncio.Task[None], ExecutionContext] | None:
        entry = self._tasks.get(run_id)
        return (entry.task, entry.context) if entry is not None else None

    # 返回当前进程内所有后台任务及上下文，用于 daemon 退出清理
    def all(self) -> list[tuple[asyncio.Task[None], ExecutionContext]]:
        return [(entry.task, entry.context) for entry in self._tasks.values()]

    # 更新 Worker 状态和结构化结果字段
    def update_status(
        self,
        worker_id: str,
        status: WorkerStatus,
        *,
        reason: str = "",
        summary: str | None = None,
        changes: list[str] | None = None,
        evidence: list[str] | None = None,
        risks: list[str] | None = None,
        blockers: list[str] | None = None,
    ) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        worker.status = status
        worker.status_reason = reason
        worker.updated_at = _now()
        if status not in ACTIVE_WORKER_STATUSES:
            worker.ended_at = worker.updated_at
        if status in {WorkerStatus.FAILED, WorkerStatus.INTERRUPTED}:
            delay = worker.retry_backoff_s * (2 ** (worker.attempt - 1))
            worker.retry_after = (
                datetime.now(UTC) + timedelta(seconds=delay)
            ).isoformat()
        if summary is not None:
            worker.summary = summary
        if changes is not None:
            worker.changes = changes
        if evidence is not None:
            worker.evidence = evidence
        if risks is not None:
            worker.risks = risks
        if blockers is not None:
            worker.blockers = blockers
        self._save(worker)
        return worker

    # 校验 backoff 和最大次数后将 interrupted/failed Worker 准备为下一 attempt
    def prepare_retry(
        self,
        worker_id: str,
        *,
        authority_ceiling: AuthoritySnapshot | None = None,
    ) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        if worker.status not in {WorkerStatus.INTERRUPTED, WorkerStatus.FAILED}:
            raise ValueError(
                f"worker status {worker.status.value} cannot be resumed or retried"
            )
        if worker.attempt >= worker.max_attempts:
            raise ValueError(f"worker retry limit reached: {worker.max_attempts}")
        if worker.retry_after and datetime.now(UTC) < datetime.fromisoformat(
            worker.retry_after
        ):
            raise ValueError(f"worker retry backoff active until {worker.retry_after}")
        self.validate_claim(worker)
        worker.attempt += 1
        worker.status = WorkerStatus.QUEUED
        worker.status_reason = ""
        worker.boot_id = self.boot_id
        worker.heartbeat_at = _now()
        worker.updated_at = worker.heartbeat_at
        worker.started_at = ""
        worker.ended_at = ""
        worker.retry_after = ""
        if authority_ceiling is not None:
            worker.authority_ceiling = authority_ceiling
        self._save(worker)
        return worker

    # 刷新 Worker 心跳并推进有界事件游标
    def heartbeat(self, worker_id: str, *, event_cursor: int | None = None) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        worker.heartbeat_at = _now()
        worker.updated_at = worker.heartbeat_at
        if event_cursor is not None:
            worker.event_cursor = max(worker.event_cursor, event_cursor)
        self._save(worker)
        return worker

    # 追加有界 Worker 进度事件并原子推进持久 event cursor
    def append_event(self, worker_id: str, kind: str, summary: str) -> WorkerEvent:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        event = WorkerEvent(
            cursor=worker.event_cursor + 1,
            worker_id=worker_id,
            kind=kind,
            summary=summary[:500],
            at=_now(),
        )
        if self._store is not None:
            self._store.append_event(event)
        worker.event_cursor = event.cursor
        worker.heartbeat_at = event.at
        worker.updated_at = event.at
        self._save(worker)
        return event

    # 从持久 backend 读取游标后的有界 Worker 事件
    def events(
        self,
        worker_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 20,
    ) -> list[WorkerEvent]:
        if self.record(worker_id) is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        if self._store is None:
            return []
        return self._store.list_events(
            worker_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    # 累加 token 使用量，并在根预算耗尽时终止全部活跃 descendant
    def add_token_usage(self, worker_id: str, tokens: int) -> bool:
        if tokens < 0:
            raise WorkerBudgetError("worker token usage must be non-negative")
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        worker.token_usage += tokens
        worker.updated_at = _now()
        self._save(worker)
        descendants = [
            item
            for item in self.list_records()
            if item.root_goal_id == worker.root_goal_id
        ]
        budget = next(
            (item.token_budget for item in descendants if item.token_budget is not None),
            None,
        )
        if budget is None:
            return False
        exhausted = sum(item.token_usage for item in descendants) >= budget
        if not exhausted:
            return False
        for item in descendants:
            if item.status not in ACTIVE_WORKER_STATUSES:
                continue
            self.update_status(
                item.id,
                WorkerStatus.BUDGET_LIMITED,
                reason=f"root goal token budget exhausted: {budget}",
            )
            live = self._tasks.get(item.id)
            if live is not None:
                live.context.status = WorkerStatus.BUDGET_LIMITED.value
                live.context.reason = "root goal token budget exhausted"
                if not live.task.done():
                    live.task.cancel()
        return True

    # 将上一 boot 或租约超时的活跃 Worker 恢复为 interrupted
    def recover_stale(self, *, now: datetime | None = None) -> list[WorkerRecord]:
        recovered: list[WorkerRecord] = []
        current = now or datetime.now(UTC)
        for worker in self.list_records():
            if worker.status not in ACTIVE_WORKER_STATUSES:
                continue
            heartbeat = datetime.fromisoformat(worker.heartbeat_at)
            lease_expired = (current - heartbeat).total_seconds() >= worker.lease_timeout_s
            if worker.boot_id == self.boot_id and not lease_expired:
                continue
            recovered.append(
                self.update_status(
                    worker.id,
                    WorkerStatus.INTERRUPTED,
                    reason="daemon restarted or worker lease expired",
                )
            )
        return recovered

    # 取消单个 Worker 及其当前进程任务句柄
    async def cancel(self, worker_id: str) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        live = self._tasks.get(worker_id)
        if live is not None:
            if not live.context.is_done():
                if self._shutting_down:
                    live.context.status = WorkerStatus.INTERRUPTED.value
                    live.context.reason = "daemon_shutdown"
                else:
                    live.context.mark_failed("cancelled")
            if not live.task.done():
                live.task.cancel()
                await asyncio.gather(live.task, return_exceptions=True)
        if self._shutting_down:
            return self.update_status(
                worker_id,
                WorkerStatus.INTERRUPTED,
                reason="daemon_shutdown",
            )
        return self.update_status(worker_id, WorkerStatus.CANCELLED, reason="cancelled")

    # 将 followup 追加到持久 prompt 和有界事件日志
    def record_followup(self, worker_id: str, message: str) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        clean = message.strip()
        if not clean:
            raise ValueError("followup message must not be empty")
        worker.prompt = (worker.prompt + "\n\nFOLLOWUP\n" + clean)[-16_000:]
        worker.updated_at = _now()
        self._save(worker)
        self.append_event(worker_id, "worker.followup", clean)
        return worker

    # 向仍在本 daemon 运行的 Worker 上下文直接注入后续指令
    def followup(self, worker_id: str, message: str) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        clean = message.strip()
        if not clean:
            raise ValueError("followup message must not be empty")
        live = self._tasks.get(worker_id)
        if live is None or live.task.done() or worker.status not in ACTIVE_WORKER_STATUSES:
            raise ValueError("worker is not running; retry or resume it before followup")
        live.context.messages.append(
            {
                "role": "user",
                "content": (
                    "Parent worker followup received. Treat this as the newest instruction:\n\n"
                    + clean
                ),
            }
        )
        return self.record_followup(worker_id, clean)

    # 递归取消指定 parent run 下的全部当前进程 descendant
    async def cancel_descendants(self, parent_run_id: str) -> None:
        descendant_ids: set[str] = set()
        frontier = {parent_run_id}
        while frontier:
            children = {
                run_id
                for run_id, entry in self._tasks.items()
                if entry.parent_run_id in frontier and run_id not in descendant_ids
            }
            descendant_ids.update(children)
            frontier = children
        for worker_id in descendant_ids:
            await self.cancel(worker_id)

    # 取消当前 daemon 内全部仍运行的 Worker
    async def cancel_all(self) -> None:
        for worker_id in list(self._tasks):
            live = self._tasks[worker_id]
            if live.task.done():
                continue
            await self.cancel(worker_id)

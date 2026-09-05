from __future__ import annotations

import asyncio
import hmac
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.context import ExecutionContext
from code_rook.core.llm.pricing import estimate_cost, resolve_pricing_quote
from code_rook.core.subagent.models import (
    ACTIVE_WORKER_STATUSES,
    WorkerEvent,
    WorkerRecord,
    WorkerStatus,
    WriteClaim,
)
from code_rook.core.subagent.store import (
    WorkerStore,
    WorkerStoreBackend,
    WorkerStoreError,
)


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


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


# 管理持久 WorkerRecord 与当前 daemon 内 asyncio 任务句柄
class BackgroundTaskRegistry:
    # 初始化 Worker 控制面，并把上一 daemon 遗留的活跃 Worker 标记为 interrupted
    def __init__(
        self,
        max_entries: int = 256,
        *,
        store_path: Path | None = None,
        store: WorkerStoreBackend | None = None,
        boot_id: str | None = None,
        recover: bool = True,
    ) -> None:
        if store_path is not None and store is not None:
            raise ValueError("store_path and store are mutually exclusive")
        self._tasks: dict[str, _BackgroundEntry] = {}
        self._memory_records: dict[str, WorkerRecord] = {}
        self._max_entries = max(1, max_entries)
        self._store = store or (WorkerStore(store_path) if store_path is not None else None)
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
        profile_digest: str = "",
        route: str = "",
        route_digest: str = "",
        model: str = "",
        reasoning: str = "",
        backend: str = "builtin",
        backend_capabilities: dict[str, JsonValue] | None = None,
        sandbox_enforcement: Literal["full", "partial", "unavailable"] = "unavailable",
        worktree: str = "",
        branch: str = "",
        base_commit: str = "",
        merge_owner: str = "",
        merge_reviewer: str = "",
        write_claim: WriteClaim | None = None,
        dependencies: list[str] | None = None,
        acceptance: list[str] | None = None,
        wall_time_s: int = 900,
        heartbeat_interval_s: float = 10.0,
        lease_timeout_s: float = 30.0,
        token_budget: int | None = None,
        root_token_budget: int | None = None,
        max_attempts: int = 3,
        retry_backoff_s: float = 1.0,
    ) -> WorkerRecord:
        now = _now()
        effective_claim = write_claim or WriteClaim(read_only=True)
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
            profile_digest=profile_digest,
            route=route,
            route_digest=route_digest,
            model=model,
            reasoning=reasoning,
            backend=backend,
            backend_capabilities=dict(backend_capabilities or {}),
            sandbox_enforcement=sandbox_enforcement,
            depth=depth,
            max_steps=max_steps,
            wall_time_s=wall_time_s,
            workspace=workspace,
            worktree=worktree,
            branch=branch,
            base_commit=base_commit,
            merge_owner=merge_owner,
            merge_reviewer=merge_reviewer,
            authority_ceiling=authority_ceiling,
            write_claim=effective_claim,
            dependencies=list(dependencies or []),
            acceptance=list(acceptance or []),
            heartbeat_at=now,
            heartbeat_interval_s=heartbeat_interval_s,
            lease_timeout_s=lease_timeout_s,
            boot_id=self.boot_id,
            token_budget=token_budget,
            root_token_budget=root_token_budget,
            max_attempts=max_attempts,
            retry_backoff_s=retry_backoff_s,
            handoff_status=("read_only" if effective_claim.read_only else "pending_execution"),
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
        if not worker.write_claim.read_only and not worker.worktree:
            raise WorkerConflictError(
                "writing worker requires an isolated managed worktree"
            )
        same_goal = [
            item
            for item in self.list_records()
            if item.root_goal_id == worker.root_goal_id
        ]
        known_budgets = {
            self._root_budget(item)
            for item in same_goal
            if self._root_budget(item) is not None
        }
        candidate_root_budget = self._root_budget(worker)
        if candidate_root_budget is not None:
            known_budgets.add(candidate_root_budget)
        if len(known_budgets) > 1:
            raise WorkerConflictError("root goal workers must share one token budget")
        shared_budget = next(iter(known_budgets), None)
        if shared_budget is not None:
            worker.root_token_budget = shared_budget
            for sibling in same_goal:
                if sibling.root_token_budget is None:
                    sibling.root_token_budget = shared_budget
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

    # 将外部 host 托管的 WorkerRecord 转换为 running 并刷新租约
    def start(self, worker_id: str) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        if worker.status != WorkerStatus.QUEUED:
            raise ValueError(f"worker status {worker.status.value} cannot start")
        now = _now()
        worker.status = WorkerStatus.RUNNING
        worker.status_reason = ""
        worker.started_at = now
        worker.heartbeat_at = now
        worker.updated_at = now
        worker.boot_id = self.boot_id
        self._save(worker)
        return worker

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
        artifact_handles: list[str] | None = None,
        approved: bool | None = None,
        receipt: dict[str, JsonValue] | None = None,
        handoff_status: str | None = None,
        changed_files: list[str] | None = None,
        diff_stat: str | None = None,
        diff_preview: str | None = None,
        diff_truncated: bool | None = None,
        verification_status: str | None = None,
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
        if artifact_handles is not None:
            worker.artifact_handles = artifact_handles
        if approved is not None:
            worker.approved = approved
        if receipt is not None:
            worker.receipt = receipt
        if handoff_status is not None:
            worker.handoff_status = handoff_status
        if changed_files is not None:
            worker.changed_files = changed_files
        if diff_stat is not None:
            worker.diff_stat = diff_stat
        if diff_preview is not None:
            worker.diff_preview = diff_preview
        if diff_truncated is not None:
            worker.diff_truncated = diff_truncated
        if verification_status is not None:
            worker.verification_status = verification_status
        self._save(worker)
        return worker

    # 返回不在 Worker 明确文件或目录 claim 内的路径，拒绝路径穿越和无边界协调声明
    @staticmethod
    def claim_violations(paths: list[str], claim: WriteClaim) -> list[str]:
        if claim.read_only or not (claim.exact_files or claim.write_roots):
            return list(paths)

        # 将 Git 路径和声明统一为不含绝对路径、点段或父目录穿越的 POSIX 形式
        def _normalized(value: str) -> str | None:
            raw = value.replace("\\", "/").strip("/")
            parts = tuple(part for part in raw.split("/") if part not in {"", "."})
            if not raw or value.startswith(("/", "\\")) or ".." in parts:
                return None
            return "/".join(parts)

        exact = {
            normalized
            for value in claim.exact_files
            if (normalized := _normalized(value)) is not None
        }
        roots = {
            normalized
            for value in claim.write_roots
            if (normalized := _normalized(value)) is not None
        }
        violations: list[str] = []
        for value in paths:
            normalized = _normalized(value)
            if normalized is None or not (
                normalized in exact
                or any(
                    normalized == root or normalized.startswith(root + "/")
                    for root in roots
                )
            ):
                violations.append(value)
        return violations

    # 记录绑定权威快照的人工审查结论但不执行任何合并
    def review_handoff(
        self,
        worker_id: str,
        *,
        approved: bool,
        review_digest: str = "",
        changed_files: list[str] | None = None,
        diff_truncated: bool | None = None,
        diff_preview: str | None = None,
    ) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        if worker.write_claim.read_only:
            raise ValueError("read-only worker has no code handoff to review")
        if worker.status != WorkerStatus.COMPLETED:
            raise ValueError("only a completed worker handoff can be reviewed")
        if worker.handoff_status not in {
            "pending_review",
            "reviewed_not_applied",
            "changes_rejected",
        }:
            raise ValueError(f"worker handoff is not reviewable: {worker.handoff_status}")
        reviewed_files = list(
            worker.changed_files if changed_files is None else changed_files
        )
        reviewed_truncated = (
            worker.diff_truncated if diff_truncated is None else diff_truncated
        )
        violations = self.claim_violations(reviewed_files, worker.write_claim)
        if approved and violations:
            raise ValueError(
                "worker handoff exceeds its write claim: " + ", ".join(violations)
            )
        if approved and reviewed_truncated:
            raise ValueError("truncated worker inspection cannot be approved for apply")
        if approved and not _DIGEST_RE.fullmatch(review_digest):
            raise ValueError("approved worker review requires an authoritative digest")
        worker.approved = approved
        worker.handoff_status = "reviewed_not_applied" if approved else "changes_rejected"
        worker.review_digest = review_digest if approved else ""
        worker.changed_files = reviewed_files
        worker.diff_truncated = reviewed_truncated
        if diff_preview is not None:
            worker.diff_preview = diff_preview
        worker.updated_at = _now()
        self._save(worker)
        self.append_event(
            worker_id,
            "worker.handoff_reviewed",
            "approved for a separate explicit apply" if approved else "changes rejected",
        )
        return worker

    # 校验 Worker 当前记录满足安全应用所需的完成、验证、审查和路径约束
    def require_applicable_handoff(
        self,
        worker_id: str,
        *,
        expected_digest: str,
        changed_files: list[str] | None = None,
        diff_truncated: bool | None = None,
    ) -> WorkerRecord:
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        if worker.write_claim.read_only:
            raise ValueError("read-only worker has no handoff to apply")
        if worker.status != WorkerStatus.COMPLETED:
            raise ValueError("only a completed worker handoff can be applied")
        if worker.handoff_status != "reviewed_not_applied" or worker.approved is not True:
            raise ValueError("worker handoff requires explicit approval before apply")
        if worker.verification_status != "verified":
            raise ValueError("worker handoff requires daemon-verified evidence before apply")
        if worker.diff_truncated or bool(diff_truncated):
            raise ValueError("truncated worker inspection cannot be applied")
        if not hmac.compare_digest(worker.review_digest, expected_digest):
            raise ValueError("worker apply digest does not match the approved review")
        current_files = list(
            worker.changed_files if changed_files is None else changed_files
        )
        if set(current_files) != set(worker.changed_files):
            raise ValueError("worker changed files no longer match the approved review")
        violations = self.claim_violations(current_files, worker.write_claim)
        if violations:
            raise ValueError(
                "worker handoff exceeds its write claim: " + ", ".join(violations)
            )
        return worker

    # 在应用成功后持久化 handoff 终态并追加可追溯 Worker 事件
    def mark_handoff_applied(
        self,
        worker_id: str,
        *,
        state_digest: str,
        changed_files: list[str],
    ) -> WorkerRecord:
        worker = self.require_applicable_handoff(
            worker_id,
            expected_digest=state_digest,
            changed_files=changed_files,
            diff_truncated=False,
        )
        worker.handoff_status = "applied"
        worker.updated_at = _now()
        self._save(worker)
        self.append_event(
            worker_id,
            "worker.handoff_applied",
            f"applied {len(changed_files)} reviewed files to the main workspace",
        )
        applied = self.record(worker_id)
        assert applied is not None
        return applied

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
        worker.approved = None
        worker.review_digest = ""
        worker.handoff_status = (
            "read_only" if worker.write_claim.read_only else "pending_execution"
        )
        worker.changed_files = []
        worker.diff_stat = ""
        worker.diff_preview = ""
        worker.diff_truncated = False
        worker.verification_status = "not_reported"
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
        worker.event_cursor = event.cursor
        worker.heartbeat_at = event.at
        worker.updated_at = event.at
        if self._store is not None:
            event = self._store.append_event_and_save(event, worker)
            worker.event_cursor = event.cursor
        else:
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

    # 返回显式根预算；旧记录没有该字段时兼容沿用原 token_budget
    @staticmethod
    def _root_budget(worker: WorkerRecord) -> int | None:
        return worker.root_token_budget or worker.token_budget

    # 累加 token 使用量，并在单 Worker 或根预算耗尽时终止对应任务
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
            (
                self._root_budget(item)
                for item in descendants
                if self._root_budget(item) is not None
            ),
            None,
        )
        root_exhausted = budget is not None and (
            sum(item.token_usage for item in descendants) >= budget
        )
        if root_exhausted:
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
        if worker.token_budget is not None and worker.token_usage >= worker.token_budget:
            self.update_status(
                worker.id,
                WorkerStatus.BUDGET_LIMITED,
                reason=f"worker token budget exhausted: {worker.token_budget}",
            )
            live = self._tasks.get(worker.id)
            if live is not None:
                live.context.status = WorkerStatus.BUDGET_LIMITED.value
                live.context.reason = "worker token budget exhausted"
                if not live.task.done():
                    live.task.cancel()
            return True
        return False

    # 累加细分 LLM usage 与可解释成本，再复用根预算硬上限
    def add_llm_usage(
        self,
        worker_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        model: str = "",
    ) -> bool:
        counts = (
            input_tokens,
            output_tokens,
            cache_read_input_tokens,
            cache_creation_input_tokens,
        )
        if any(value < 0 for value in counts):
            raise WorkerBudgetError("worker LLM usage must be non-negative")
        exhausted = self.add_token_usage(
            worker_id,
            input_tokens + output_tokens,
        )
        worker = self.record(worker_id)
        if worker is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        worker.input_tokens += input_tokens
        worker.output_tokens += output_tokens
        worker.cache_read_input_tokens += cache_read_input_tokens
        worker.cache_creation_input_tokens += cache_creation_input_tokens
        quote = resolve_pricing_quote(model or worker.model)
        if quote is None or worker.cost_status == "unknown":
            worker.estimated_cost_usd = None
            worker.cost_status = "unknown"
        else:
            increment = estimate_cost(
                quote.pricing,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_input_tokens,
                cache_write_tokens=cache_creation_input_tokens,
            )
            worker.estimated_cost_usd = (worker.estimated_cost_usd or 0.0) + increment
            worker.cost_status = "estimated"
        worker.updated_at = _now()
        self._save(worker)
        return exhausted

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

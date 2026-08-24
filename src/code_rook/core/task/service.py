from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from pydantic import JsonValue

from code_rook.core.task.models import (
    TaskArtifact,
    TaskAttempt,
    TaskGate,
    TaskRecord,
    TaskStatus,
    TaskTimelineEntry,
)
from code_rook.core.task.store import TaskStore

TaskEventSink = Callable[[TaskTimelineEntry], None]


# 返回当前 UTC 时间的稳定 ISO 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将旧工具状态名归一化为 Task V2 状态
def _normalize_status(status: str) -> TaskStatus:
    normalized = {"in_progress": "running"}.get(status, status)
    allowed = {
        "pending",
        "ready",
        "running",
        "blocked",
        "completed",
        "failed",
        "cancelled",
    }
    if normalized not in allowed:
        raise ValueError(f"invalid status: {status!r}")
    return cast(TaskStatus, normalized)


class TaskService:
    # 初始化持久任务服务和可选的 runtime timeline 投影回调
    def __init__(
        self,
        store: TaskStore,
        *,
        event_sink: TaskEventSink | None = None,
    ) -> None:
        self._store = store
        self._event_sink = event_sink

    # 向任务追加单调 timeline entry，并同步投影到外部事件 sink
    def _record(
        self,
        task: TaskRecord,
        event: str,
        actor: str,
        details: dict[str, JsonValue] | None = None,
    ) -> TaskTimelineEntry:
        entry = TaskTimelineEntry(
            task_id=task.id,
            seq=len(task.timeline) + 1,
            event=event,
            actor=actor.strip() or "agent",
            at=_now(),
            details=details or {},
        )
        task.timeline.append(entry)
        task.updated_at = entry.at
        task.updated_by = entry.actor
        return entry

    # 在任务记录持久化成功后投影 timeline event，避免失败写入产生幽灵事件
    def _emit(self, entry: TaskTimelineEntry) -> None:
        if self._event_sink is not None:
            self._event_sink(entry)

    # 检查全部依赖任务是否已经成功完成
    def _dependencies_completed(self, task: TaskRecord) -> bool:
        return all(self._store.get(task_id).status == "completed" for task_id in task.dependencies)

    # 校验候选依赖子图保持无环，拒绝直接或间接回指当前任务
    def _ensure_acyclic(self, task_id: int, dependencies: list[int]) -> None:
        visiting: set[int] = set()
        visited: set[int] = set()

        # 深度优先检查依赖边，并在回到当前递归栈时报告完整环路
        def visit(current_id: int, path: list[int]) -> None:
            if current_id in visiting:
                cycle_start = path.index(current_id)
                cycle = [*path[cycle_start:], current_id]
                raise ValueError(
                    "task dependency cycle: " + " -> ".join(str(item) for item in cycle)
                )
            if current_id in visited:
                return
            visiting.add(current_id)
            current_dependencies = (
                dependencies
                if current_id == task_id
                else self._store.get(current_id).dependencies
            )
            for dependency_id in current_dependencies:
                visit(dependency_id, [*path, current_id])
            visiting.remove(current_id)
            visited.add(current_id)

        visit(task_id, [])

    # 创建带依赖、验收条件和 gate 的持久任务
    def create(
        self,
        subject: str,
        description: str = "",
        *,
        dependencies: list[int] | None = None,
        acceptance_criteria: list[str] | None = None,
        gates: list[str] | None = None,
        actor: str = "agent",
    ) -> TaskRecord:
        clean_subject = subject.strip()
        if not clean_subject:
            raise ValueError("task subject must not be empty")
        dependency_ids = list(dict.fromkeys(dependencies or []))
        for dependency_id in dependency_ids:
            self._store.get(dependency_id)
        # 在 store 的同一临界区内分配 ID、构造 timeline 并首次落盘
        def build(task_id: int) -> TaskRecord:
            now = _now()
            task = TaskRecord(
                id=task_id,
                subject=clean_subject,
                description=description,
                status="ready" if not dependency_ids else "blocked",
                dependencies=dependency_ids,
                acceptance_criteria=[
                    item.strip() for item in acceptance_criteria or [] if item.strip()
                ],
                gates=[
                    TaskGate(name=name.strip(), updated_at=now)
                    for name in gates or []
                    if name.strip()
                ],
                created_by=actor.strip() or "agent",
                updated_by=actor.strip() or "agent",
                created_at=now,
                updated_at=now,
            )
            self._ensure_acyclic(task.id, task.dependencies)
            if dependency_ids and self._dependencies_completed(task):
                task.status = "ready"
            self._record(
                task,
                "task.created",
                actor,
                {
                    "status": task.status,
                    "dependencies": cast(JsonValue, dependency_ids),
                },
            )
            return task

        task = self._store.create(build)
        self._emit(task.timeline[-1])
        return task

    # 原子认领依赖已满足的 ready 任务并开始新的 attempt
    def claim(
        self,
        task_id: int,
        owner_worker: str,
        worktree: str = "",
        *,
        actor: str | None = None,
    ) -> TaskRecord:
        owner = owner_worker.strip()
        if not owner:
            raise ValueError("task owner must not be empty")

        # 在 TaskStore 写锁内完成状态检查、attempt 创建和写回
        def mutate(task: TaskRecord) -> TaskRecord:
            if task.status in {"pending", "blocked"} and self._dependencies_completed(task):
                task.status = "ready"
            if task.status != "ready":
                if not self._dependencies_completed(task):
                    blocked = [
                        dependency_id
                        for dependency_id in task.dependencies
                        if self._store.get(dependency_id).status != "completed"
                    ]
                    raise ValueError(f"task {task_id} is blocked by {blocked}")
                raise ValueError(f"task {task_id} is {task.status}, cannot claim")
            task.owner_worker = owner
            task.worktree = worktree.strip()
            task.status = "running"
            attempt = TaskAttempt(
                number=len(task.attempts) + 1,
                owner_worker=owner,
                started_at=_now(),
            )
            task.attempts.append(attempt)
            self._record(
                task,
                "task.claimed",
                actor or owner,
                {"attempt": attempt.number, "owner_worker": owner},
            )
            return task

        updated = self._store.mutate(task_id, mutate)
        self._emit(updated.timeline[-1])
        return updated

    # 返回指定任务的完整持久记录
    def get(self, task_id: int) -> TaskRecord:
        return self._store.get(task_id)

    # 返回全部任务并刷新可由依赖完成而进入 ready 的任务
    def list_all(self) -> list[TaskRecord]:
        self._refresh_ready_tasks()
        return self._store.list()

    # 更新任务状态或依赖，并为所有变化追加 timeline
    def update(
        self,
        task_id: int,
        *,
        status: str | None = None,
        add_dependencies: list[int] | None = None,
        remove_dependencies: list[int] | None = None,
        actor: str = "agent",
    ) -> TaskRecord:
        additions = list(dict.fromkeys(add_dependencies or []))
        removals = set(remove_dependencies or [])
        for dependency_id in additions:
            if dependency_id == task_id:
                raise ValueError("task cannot depend on itself")
            self._store.get(dependency_id)

        # 在单次原子变更中更新依赖、状态和当前 attempt 终态
        def mutate(task: TaskRecord) -> TaskRecord:
            candidate_dependencies = [
                dependency_id
                for dependency_id in dict.fromkeys([*task.dependencies, *additions])
                if dependency_id not in removals
            ]
            self._ensure_acyclic(task_id, candidate_dependencies)
            task.dependencies = candidate_dependencies
            requested = _normalize_status(status) if status is not None else task.status
            if requested == "completed" and any(gate.status != "passed" for gate in task.gates):
                raise ValueError(f"task {task_id} has gates that have not passed")
            if requested == "running" and not task.attempts:
                task.attempts.append(
                    TaskAttempt(
                        number=1,
                        owner_worker=task.owner_worker or actor,
                        started_at=_now(),
                    )
                )
            if requested in {"completed", "failed", "cancelled"} and task.attempts:
                attempt = task.attempts[-1]
                if attempt.status == "running":
                    attempt.status = requested
                    attempt.ended_at = _now()
            task.status = requested
            if task.status in {"pending", "blocked", "ready"}:
                task.status = "ready" if self._dependencies_completed(task) else "blocked"
            self._record(
                task,
                "task.updated",
                actor,
                {
                    "status": task.status,
                    "dependencies": list(task.dependencies),
                },
            )
            return task

        updated = self._store.mutate(task_id, mutate)
        self._emit(updated.timeline[-1])
        if updated.status == "completed":
            self._refresh_ready_tasks()
        return updated

    # 为任务登记内容寻址产物并写入 timeline
    def add_artifact(
        self,
        task_id: int,
        *,
        name: str,
        uri: str,
        digest: str = "",
        media_type: str = "application/octet-stream",
        actor: str = "agent",
    ) -> TaskRecord:
        # 原子追加 artifact，避免与状态更新互相覆盖
        def mutate(task: TaskRecord) -> TaskRecord:
            artifact = TaskArtifact(
                name=name,
                uri=uri,
                digest=digest,
                media_type=media_type,
                created_at=_now(),
            )
            task.artifacts.append(artifact)
            self._record(
                task,
                "task.artifact_added",
                actor,
                {"name": artifact.name, "uri": artifact.uri, "digest": artifact.digest},
            )
            return task

        updated = self._store.mutate(task_id, mutate)
        self._emit(updated.timeline[-1])
        return updated

    # 设置命名 gate 的终态与证据，未知 gate 明确拒绝
    def set_gate(
        self,
        task_id: int,
        gate_name: str,
        *,
        passed: bool,
        evidence: str,
        actor: str = "agent",
    ) -> TaskRecord:
        # 原子更新 gate 及其审计 timeline
        def mutate(task: TaskRecord) -> TaskRecord:
            gate = next((item for item in task.gates if item.name == gate_name), None)
            if gate is None:
                raise ValueError(f"task {task_id} gate not found: {gate_name}")
            gate.status = "passed" if passed else "failed"
            gate.evidence = evidence
            gate.updated_at = _now()
            self._record(
                task,
                "task.gate_updated",
                actor,
                {"gate": gate.name, "status": gate.status},
            )
            return task

        updated = self._store.mutate(task_id, mutate)
        self._emit(updated.timeline[-1])
        return updated

    # 返回指定任务的完整审计 timeline
    def timeline(self, task_id: int) -> tuple[TaskTimelineEntry, ...]:
        return tuple(self._store.get(task_id).timeline)

    # 刷新所有 blocked/pending 任务的 ready 状态但保留依赖历史
    def _refresh_ready_tasks(self) -> None:
        for task in self._store.list():
            if task.status not in {"pending", "blocked"}:
                continue
            if not self._dependencies_completed(task):
                continue
            task.status = "ready"
            self._record(
                task,
                "task.ready",
                "system",
                {"dependencies": cast(JsonValue, task.dependencies)},
            )
            self._store.save(task)
            self._emit(task.timeline[-1])

    # 格式化任务控制面摘要供 Agent 和 TUI 使用
    def format_list(self) -> str:
        tasks = self.list_all()
        if not tasks:
            return "No tasks."
        markers = {
            "pending": "[ ]",
            "ready": "[ ]",
            "running": "[>]",
            "blocked": "[-]",
            "completed": "[x]",
            "failed": "[!]",
            "cancelled": "[-]",
        }
        lines = []
        for task in tasks:
            blocked = (
                f" (dependencies: {task.dependencies})" if task.dependencies else ""
            )
            lines.append(f"{markers[task.status]} #{task.id}: {task.subject}{blocked}")
        return "\n".join(lines)

    # 返回注入 system prompt 的当前 Task 状态摘要
    def active_summary(self) -> str:
        summary = self.format_list()
        return "" if summary == "No tasks." else f"## Todo State\n{summary}"

    # 仅 active 状态任务阻止 Agent 过早结束，失败和取消视为明确终态
    def has_incomplete(self) -> bool:
        return any(
            task.status in {"pending", "ready", "running", "blocked"}
            for task in self.list_all()
        )

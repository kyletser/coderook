from __future__ import annotations

from pathlib import Path
from typing import Protocol

from code_rook.core.task.models import TaskRecord, TaskTimelineEntry
from code_rook.core.task.service import TaskEventSink, TaskService
from code_rook.core.task.store import TaskStore


# 软状态机视图：loop 据此把 todos 注入 system prompt，并在 end_turn 完成性检查时使用
class TodoStateView(Protocol):
    # 返回当前 todos 的简短文本摘要，供 loop 拼到 system prompt 末尾；无 todos 返回空串
    def active_summary(self) -> str: ...

    # 返回 True 表示仍有未完成 (pending/in_progress) 的 todos
    def has_incomplete(self) -> bool: ...


class TaskManager:
    # 初始化 V2 task store，并保留旧工具调用的 facade 参数
    def __init__(
        self,
        tasks_dir: Path,
        *,
        event_sink: TaskEventSink | None = None,
    ) -> None:
        self._dir = tasks_dir
        self._task_store = TaskStore(tasks_dir)
        self._service = TaskService(self._task_store, event_sink=event_sink)

    # 使用旧 blocked_by 名称创建 V2 任务
    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
        *,
        acceptance_criteria: list[str] | None = None,
        gates: list[str] | None = None,
    ) -> TaskRecord:
        return self._service.create(
            subject,
            description,
            dependencies=blocked_by,
            acceptance_criteria=acceptance_criteria,
            gates=gates,
        )

    # 使用旧依赖参数名更新 V2 任务并保留完整依赖历史
    def update(
        self,
        task_id: int,
        *,
        status: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> TaskRecord:
        return self._service.update(
            task_id,
            status=status,
            add_dependencies=add_blocked_by,
            remove_dependencies=remove_blocked_by,
        )

    # 暴露 V2 timeline 类型供旧调用方查询
    def timeline(self, task_id: int) -> tuple[TaskTimelineEntry, ...]:
        return self._service.timeline(task_id)

    # 原子认领依赖已经满足的任务
    def claim(
        self,
        task_id: int,
        owner_worker: str,
        worktree: str = "",
    ) -> TaskRecord:
        return self._service.claim(task_id, owner_worker, worktree)

    # 返回指定任务的完整持久记录
    def get(self, task_id: int) -> TaskRecord:
        return self._service.get(task_id)

    # 返回全部任务并刷新可执行状态
    def list_all(self) -> list[TaskRecord]:
        return self._service.list_all()

    # 为任务登记产物引用和摘要信息
    def add_artifact(
        self,
        task_id: int,
        *,
        name: str,
        uri: str,
        digest: str = "",
        media_type: str = "application/octet-stream",
    ) -> TaskRecord:
        return self._service.add_artifact(
            task_id,
            name=name,
            uri=uri,
            digest=digest,
            media_type=media_type,
        )

    # 设置任务 gate 的校验结果与证据
    def set_gate(
        self,
        task_id: int,
        gate_name: str,
        *,
        passed: bool,
        evidence: str,
    ) -> TaskRecord:
        return self._service.set_gate(
            task_id,
            gate_name,
            passed=passed,
            evidence=evidence,
        )

    # 格式化任务控制面摘要供 Agent 和 TUI 使用
    def format_list(self) -> str:
        return self._service.format_list()

    # 返回注入 system prompt 的当前任务状态摘要
    def active_summary(self) -> str:
        return self._service.active_summary()

    # 判断是否仍有阻止当前执行结束的活动任务
    def has_incomplete(self) -> bool:
        return self._service.has_incomplete()

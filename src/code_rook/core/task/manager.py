from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from code_rook.core.task.model import Task, TaskStatus


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 软状态机视图：loop 据此把 todos 注入 system prompt，并在 end_turn 完成性检查时使用
class TodoStateView(Protocol):
    # 返回当前 todos 的简短文本摘要，供 loop 拼到 system prompt 末尾；无 todos 返回空串
    def active_summary(self) -> str: ...

    # 返回 True 表示仍有未完成 (pending/in_progress) 的 todos
    def has_incomplete(self) -> bool: ...


class TaskManager:
    # 初始化：确保目录存在，扫描现有文件确定下一个 ID
    def __init__(self, tasks_dir: Path) -> None:
        self._dir = tasks_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    # 扫描目录中 task_*.json 文件，返回最大 ID（无文件则返回 0）
    def _max_id(self) -> int:
        ids = [
            int(f.stem.split("_")[1])
            for f in self._dir.glob("task_*.json")
            if f.stem.split("_")[1].isdigit()
        ]
        return max(ids) if ids else 0

    # 读取指定 ID 的任务文件
    def _load(self, task_id: int) -> Task:
        path = self._dir / f"task_{task_id}.json"
        if not path.exists():
            raise ValueError(f"task {task_id} not found")
        return Task.from_dict(json.loads(path.read_text()))

    # 将任务写入对应 JSON 文件
    def _save(self, task: Task) -> None:
        path = self._dir / f"task_{task.id}.json"
        path.write_text(json.dumps(task.to_dict(), indent=2, ensure_ascii=False))

    # 创建新任务，写入 JSON 文件，返回 Task
    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
    ) -> Task:
        for dep_id in (blocked_by or []):
            if not (self._dir / f"task_{dep_id}.json").exists():
                raise ValueError(f"blocked_by task {dep_id} not found")
        now = _now()
        task = Task(
            id=self._next_id,
            subject=subject,
            description=description,
            status="pending",
            blocked_by=list(blocked_by or []),
            owner="",
            worktree="",
            created_at=now,
            updated_at=now,
        )
        self._save(task)
        self._next_id += 1
        return task

    # 原子认领可执行的 pending 任务，并记录 owner 和可选 worktree
    def claim(self, task_id: int, owner: str, worktree: str = "") -> Task:
        clean_owner = owner.strip()
        if not clean_owner:
            raise ValueError("task owner must not be empty")
        task = self._load(task_id)
        if task.status != "pending":
            raise ValueError(f"task {task_id} is {task.status}, cannot claim")
        if task.owner:
            raise ValueError(f"task {task_id} already owned by {task.owner}")
        blocked = [dep_id for dep_id in task.blocked_by if self._load(dep_id).status != "completed"]
        if blocked:
            raise ValueError(f"task {task_id} is blocked by {blocked}")
        task.owner = clean_owner
        task.worktree = worktree.strip()
        task.status = "in_progress"
        task.updated_at = _now()
        self._save(task)
        return task

    # 读取指定 ID 的任务
    def get(self, task_id: int) -> Task:
        return self._load(task_id)

    # 更新任务状态或依赖列表；status="completed" 时自动清理其他任务的 blocked_by
    def update(
        self,
        task_id: int,
        *,
        status: TaskStatus | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> Task:
        task = self._load(task_id)
        if status is not None:
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"invalid status: {status!r}")
            task.status = status
            if status == "completed":
                self._clear_dependency(task_id)
        if add_blocked_by:
            task.blocked_by = list(set(task.blocked_by + add_blocked_by))
        if remove_blocked_by:
            task.blocked_by = [x for x in task.blocked_by if x not in remove_blocked_by]
        task.updated_at = _now()
        self._save(task)
        return task

    # 返回所有任务，按 ID 升序排列
    def list_all(self) -> list[Task]:
        tasks = []
        for f in sorted(self._dir.glob("task_*.json"), key=lambda p: int(p.stem.split("_")[1])):
            try:
                tasks.append(Task.from_dict(json.loads(f.read_text())))
            except (ValueError, KeyError):
                pass
        return tasks

    # 将 completed_id 从所有其他任务的 blocked_by 列表中移除
    def _clear_dependency(self, completed_id: int) -> None:
        for f in self._dir.glob("task_*.json"):
            try:
                data = json.loads(f.read_text())
            except (ValueError, json.JSONDecodeError):
                continue
            blocked = [int(x) for x in data.get("blocked_by", [])]
            if completed_id in blocked:
                data["blocked_by"] = [x for x in blocked if x != completed_id]
                data["updated_at"] = _now()
                f.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # 格式化任务列表摘要，供 task_list 工具返回给 Agent
    def format_list(self) -> str:
        tasks = self.list_all()
        if not tasks:
            return "No tasks."
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = []
        for t in tasks:
            blocked = f" (blocked by: {t.blocked_by})" if t.blocked_by else ""
            lines.append(f"{marker.get(t.status, '[?]')} #{t.id}: {t.subject}{blocked}")
        return "\n".join(lines)

    # 摘要当前所有 todos，供 loop 在 system prompt 中作为软状态机注入；无任何任务返回空
    def active_summary(self) -> str:
        tasks = self.list_all()
        if not tasks:
            return ""
        marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
        lines = ["## Todo State"]
        for t in tasks:
            blocked = f" (blocked by: {t.blocked_by})" if t.blocked_by else ""
            lines.append(f"{marker.get(t.status, '[?]')} #{t.id}: {t.subject}{blocked}")
        return "\n".join(lines)

    # 存在 status != "completed" 的 todos 时返回 True
    def has_incomplete(self) -> bool:
        return any(t.status != "completed" for t in self.list_all())

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from code_rook.core.task.models import TaskRecord


class TaskStoreError(ValueError):
    pass


class TaskStore:
    # 初始化任务目录和进程内写锁，单 daemon 内保证 claim 原子性
    def __init__(self, tasks_dir: Path) -> None:
        self.path = tasks_dir
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    # 返回任务 JSON 文件的稳定路径
    def task_path(self, task_id: int) -> Path:
        return self.path / f"task_{task_id}.json"

    # 扫描持久记录并返回下一个单调递增 ID
    def next_id(self) -> int:
        ids = [
            int(path.stem.split("_")[1])
            for path in self.path.glob("task_*.json")
            if path.stem.split("_")[1].isdigit()
        ]
        return max(ids, default=0) + 1

    # 读取并迁移指定任务记录
    def get(self, task_id: int) -> TaskRecord:
        target = self.task_path(task_id)
        if not target.exists():
            raise TaskStoreError(f"task {task_id} not found")
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("task document must be an object")
            return TaskRecord.from_dict(raw)
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise TaskStoreError(f"invalid task {task_id}: {exc}") from exc

    # 按 ID 稳定排序读取所有合法任务，损坏记录明确失败而不静默跳过
    def list(self) -> list[TaskRecord]:
        return [
            self.get(int(path.stem.split("_")[1]))
            for path in sorted(
                self.path.glob("task_*.json"),
                key=lambda item: int(item.stem.split("_")[1]),
            )
        ]

    # 原子替换保存完整版本化任务记录
    def save(self, task: TaskRecord) -> None:
        target = self.task_path(task.id)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        with self._lock:
            temporary.write_text(
                json.dumps(task.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)

    # 在同一进程锁内执行读改写事务并返回更新记录
    def mutate(
        self,
        task_id: int,
        mutation: Callable[[TaskRecord], TaskRecord],
    ) -> TaskRecord:
        with self._lock:
            task = self.get(task_id)
            updated = mutation(task)
            if not isinstance(updated, TaskRecord):
                raise TaskStoreError("task mutation returned an invalid record")
            self.save(updated)
            return updated

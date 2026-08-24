from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from code_rook.core.quarantine import quarantine_invalid_file
from code_rook.core.task.models import TaskRecord, UnsupportedTaskSchemaError

_TASK_FILE_RE = re.compile(r"^task_(\d+)\.json$")
logger = logging.getLogger(__name__)


class TaskStoreError(ValueError):
    pass


class TaskStoreUnsupportedVersion(TaskStoreError):
    pass


class TaskStore:
    # 初始化任务目录和进程内写锁，单 daemon 内保证 claim 原子性
    def __init__(self, tasks_dir: Path) -> None:
        self.path = tasks_dir.expanduser().absolute()
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_dir()):
            raise TaskStoreError("task state root must be a real directory")
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    # 返回任务 JSON 文件的稳定路径
    def task_path(self, task_id: int) -> Path:
        return self.path / f"task_{task_id}.json"

    # 扫描持久记录并返回下一个单调递增 ID
    def next_id(self) -> int:
        with self._lock:
            ids = [
                int(path.stem.split("_")[1])
                for path in self.path.glob("task_*.json")
                if path.stem.split("_")[1].isdigit()
            ]
            return max(ids, default=0) + 1

    # 读取并迁移指定任务记录
    def get(self, task_id: int) -> TaskRecord:
        target = self.task_path(task_id)
        if target.is_symlink() or not target.is_file():
            raise TaskStoreError(f"task {task_id} not found")
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("task document must be an object")
            record = TaskRecord.from_dict(raw)
            if record.id != task_id:
                raise ValueError("task record id does not match its filename")
            return record
        except UnsupportedTaskSchemaError as exc:
            raise TaskStoreUnsupportedVersion(str(exc)) from exc
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise TaskStoreError(f"invalid task {task_id}: {exc}") from exc

    # 按 ID 稳定读取合法任务，并把单条损坏记录隔离后继续
    def list(self) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        for path in sorted(self.path.glob("task_*.json")):
            matched = _TASK_FILE_RE.fullmatch(path.name)
            try:
                if matched is None:
                    raise TaskStoreError("invalid task filename")
                records.append(self.get(int(matched.group(1))))
            except TaskStoreUnsupportedVersion:
                logger.warning("skip unsupported future task record: %s", path)
            except TaskStoreError:
                quarantined = quarantine_invalid_file(
                    path,
                    category="task",
                    reason="record failed strict TaskRecord validation",
                    state_root=self.path,
                )
                logger.warning(
                    "isolated invalid task record: %s",
                    quarantined or path,
                )
        return sorted(records, key=lambda item: item.id)

    # 原子替换保存完整版本化任务记录
    def save(self, task: TaskRecord) -> None:
        with self._lock:
            self._save_locked(task)

    # 在同一写锁中分配 ID、构造记录并首次落盘，避免并发 create 复用同一 ID
    def create(self, factory: Callable[[int], TaskRecord]) -> TaskRecord:
        with self._lock:
            task = factory(self.next_id())
            target = self.task_path(task.id)
            if target.exists():
                raise TaskStoreError(f"task {task.id} already exists")
            self._save_locked(task)
            return task

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
            self._save_locked(updated)
            return updated

    # 在调用方已持有写锁时原子替换任务文件，避免重复嵌套锁和临时文件竞争
    def _save_locked(self, task: TaskRecord) -> None:
        target = self.task_path(task.id)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

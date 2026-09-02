from __future__ import annotations

import builtins
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Protocol

from pydantic import ValidationError

from code_rook.core.subagent.models import WorkerEvent, WorkerRecord


class WorkerStoreError(ValueError):
    pass


class WorkerStoreBackend(Protocol):
    # 保存一个完整 WorkerRecord
    def save(self, worker: WorkerRecord) -> None: ...

    # 读取指定 WorkerRecord
    def get(self, worker_id: str) -> WorkerRecord: ...

    # 稳定列出全部 WorkerRecord
    def list(self) -> list[WorkerRecord]: ...

    # 追加一条 WorkerEvent
    def append_event(self, event: WorkerEvent) -> None: ...

    # 将 WorkerEvent 与推进后的 WorkerRecord 一起持久化
    def append_event_and_save(
        self,
        event: WorkerEvent,
        worker: WorkerRecord,
    ) -> WorkerEvent: ...

    # 从游标后读取有界 WorkerEvent
    def list_events(
        self,
        worker_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 20,
    ) -> builtins.list[WorkerEvent]: ...


class WorkerStore:
    # 初始化 Worker 持久目录和单进程原子写锁
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    # 返回指定 Worker 的稳定 JSON 路径
    def worker_path(self, worker_id: str) -> Path:
        safe_id = worker_id.replace("/", "_").replace("\\", "_")
        return self.path / f"{safe_id}.json"

    # 返回指定 Worker 的追加式有界事件日志路径
    def event_path(self, worker_id: str) -> Path:
        safe_id = worker_id.replace("/", "_").replace("\\", "_")
        return self.path / f"{safe_id}.events.jsonl"

    # 原子保存完整 WorkerRecord
    def save(self, worker: WorkerRecord) -> None:
        target = self.worker_path(worker.id)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        with self._lock:
            temporary.write_text(
                worker.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)

    # 读取并严格校验指定 WorkerRecord
    def get(self, worker_id: str) -> WorkerRecord:
        target = self.worker_path(worker_id)
        if not target.exists():
            raise WorkerStoreError(f"worker not found: {worker_id}")
        try:
            return WorkerRecord.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise WorkerStoreError(f"invalid worker {worker_id}: {exc}") from exc

    # 按创建时间和 ID 稳定列出全部 WorkerRecord
    def list(self) -> list[WorkerRecord]:
        records = [
            self.get(path.stem)
            for path in self.path.glob("*.json")
            if not path.name.endswith(".events.json")
        ]
        return sorted(records, key=lambda item: (item.created_at, item.id))

    # 追加一条已经过摘要和长度限制的 Worker 事件
    def append_event(self, event: WorkerEvent) -> None:
        target = self.event_path(event.worker_id)
        with self._lock, target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(event.model_dump_json() + "\n")

    # 在进程内写锁中连续追加事件和快照，避免并发调用观察到游标倒退
    def append_event_and_save(
        self,
        event: WorkerEvent,
        worker: WorkerRecord,
    ) -> WorkerEvent:
        with self._lock:
            self.append_event(event)
            self.save(worker)
        return event

    # 从游标后读取至多 limit 条 Worker 事件
    def list_events(
        self,
        worker_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 20,
    ) -> builtins.list[WorkerEvent]:
        target = self.event_path(worker_id)
        if not target.exists():
            return []
        events: builtins.list[WorkerEvent] = []
        try:
            for line in target.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = WorkerEvent.model_validate_json(line)
                if event.cursor > after_cursor:
                    events.append(event)
                if len(events) >= max(1, min(limit, 100)):
                    break
        except (OSError, ValidationError, ValueError) as exc:
            raise WorkerStoreError(f"invalid worker event log {worker_id}: {exc}") from exc
        return events

    # 在同一写锁内执行 Worker 读改写事务
    def mutate(
        self,
        worker_id: str,
        mutation: Callable[[WorkerRecord], WorkerRecord],
    ) -> WorkerRecord:
        with self._lock:
            worker = self.get(worker_id)
            updated = mutation(worker)
            if not isinstance(updated, WorkerRecord):
                raise WorkerStoreError("worker mutation returned an invalid record")
            self.save(updated)
            return updated

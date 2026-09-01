from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from code_rook.core.subagent.models import WorkerEvent, WorkerRecord
from code_rook.core.subagent.store import WorkerStoreError


class SQLiteWorkerStore:
    # 初始化 Fleet SQLite ledger 并创建 Worker 与 event 表
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    # 打开启用 WAL、foreign key 和 busy timeout 的 SQLite 连接
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # 创建 Fleet Worker snapshot 与追加式 event ledger
    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_workers (
                    id TEXT PRIMARY KEY,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fleet_worker_events (
                    worker_id TEXT NOT NULL,
                    cursor INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    at TEXT NOT NULL,
                    PRIMARY KEY(worker_id, cursor),
                    FOREIGN KEY(worker_id) REFERENCES fleet_workers(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS fleet_worker_events_cursor_idx
                    ON fleet_worker_events(worker_id, cursor);
                """
            )

    # 插入或原子更新一个完整 WorkerRecord snapshot
    def save(self, worker: WorkerRecord) -> None:
        payload = worker.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fleet_workers(id, record_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    record_json = excluded.record_json,
                    updated_at = excluded.updated_at
                """,
                (worker.id, payload, worker.created_at, worker.updated_at),
            )

    # 从 SQLite 读取并严格校验一个 WorkerRecord
    def get(self, worker_id: str) -> WorkerRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM fleet_workers WHERE id = ?",
                (worker_id,),
            ).fetchone()
        if row is None:
            raise WorkerStoreError(f"worker not found: {worker_id}")
        try:
            return WorkerRecord.model_validate_json(str(row["record_json"]))
        except (ValidationError, ValueError) as exc:
            raise WorkerStoreError(f"invalid worker {worker_id}: {exc}") from exc

    # 按创建时间与 ID 稳定列出全部 Fleet Worker
    def list(self) -> list[WorkerRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM fleet_workers ORDER BY created_at, id"
            ).fetchall()
        try:
            return [
                WorkerRecord.model_validate_json(str(row["record_json"])) for row in rows
            ]
        except (ValidationError, ValueError) as exc:
            raise WorkerStoreError(f"invalid fleet worker ledger: {exc}") from exc

    # 追加一条具有唯一 worker cursor 的 durable WorkerEvent
    def append_event(self, event: WorkerEvent) -> None:
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO fleet_worker_events(worker_id, cursor, event_json, at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event.worker_id, event.cursor, event.model_dump_json(), event.at),
                )
            except sqlite3.IntegrityError as exc:
                raise WorkerStoreError(
                    f"duplicate or orphan fleet event: {event.worker_id}:{event.cursor}"
                ) from exc

    # 从指定 cursor 后稳定读取有界 Fleet WorkerEvent
    def list_events(
        self,
        worker_id: str,
        *,
        after_cursor: int = 0,
        limit: int = 20,
    ) -> builtins.list[WorkerEvent]:
        bounded_limit = max(1, min(limit, 100))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_json FROM fleet_worker_events "
                "WHERE worker_id = ? AND cursor > ? ORDER BY cursor LIMIT ?",
                (worker_id, after_cursor, bounded_limit),
            ).fetchall()
        try:
            return [
                WorkerEvent.model_validate(json.loads(str(row["event_json"])))
                for row in rows
            ]
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise WorkerStoreError(
                f"invalid fleet event ledger {worker_id}: {exc}"
            ) from exc

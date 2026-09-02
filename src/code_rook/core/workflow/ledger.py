from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from pydantic import JsonValue, ValidationError

from code_rook.core.workflow.graph import (
    GraphNodeKind,
    WorkflowEvent,
    WorkGraphState,
    apply_workflow_event,
    reduce_workflow_events,
)
from code_rook.core.workflow.models import WorkflowSpec


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class WorkflowLedgerError(ValueError):
    pass


class WorkflowLedger:
    # 初始化 SQLite ledger 并创建版本化 workflow/event 表
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # workflow_id -> (已回放到的 seq, 对应 Work Graph 快照)，供增量重建
        self._graph_cache: dict[str, tuple[int, WorkGraphState]] = {}
        self._initialize()

    # 打开启用 foreign key、WAL 和 busy timeout 的 SQLite 连接
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

    # 创建 workflow 定义、状态和追加式事件表
    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflows (
                    id TEXT PRIMARY KEY,
                    spec_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    receipt_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_events (
                    workflow_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_kind TEXT,
                    details_json TEXT NOT NULL,
                    at TEXT NOT NULL,
                    PRIMARY KEY (workflow_id, seq),
                    FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS workflow_events_kind_idx
                    ON workflow_events(workflow_id, kind, seq);
                CREATE TABLE IF NOT EXISTS workflow_event_counters (
                    workflow_id TEXT PRIMARY KEY,
                    next_seq INTEGER NOT NULL,
                    FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
                );
                INSERT OR IGNORE INTO workflow_event_counters(workflow_id, next_seq)
                SELECT workflows.id, COALESCE(MAX(workflow_events.seq), 0) + 1
                FROM workflows
                LEFT JOIN workflow_events ON workflow_events.workflow_id = workflows.id
                GROUP BY workflows.id;
                """
            )

    # 保存新 WorkflowSpec，已存在的相同定义保持幂等，不允许静默替换
    def create(self, spec: WorkflowSpec) -> None:
        payload = spec.model_dump_json()
        now = _now()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json FROM workflows WHERE id = ?",
                (spec.id,),
            ).fetchone()
            if row is not None:
                if str(row["spec_json"]) != payload:
                    raise WorkflowLedgerError(f"workflow already exists: {spec.id}")
                return
            connection.execute(
                """
                INSERT INTO workflows(id, spec_json, status, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?)
                """,
                (spec.id, payload, now, now),
            )
            connection.execute(
                "INSERT INTO workflow_event_counters(workflow_id, next_seq) VALUES (?, 1)",
                (spec.id,),
            )

    # 读取并严格校验持久 WorkflowSpec
    def get_spec(self, workflow_id: str) -> WorkflowSpec:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT spec_json FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
        if row is None:
            raise WorkflowLedgerError(f"workflow not found: {workflow_id}")
        try:
            return WorkflowSpec.model_validate_json(str(row["spec_json"]))
        except (ValidationError, ValueError) as exc:
            raise WorkflowLedgerError(f"invalid workflow {workflow_id}: {exc}") from exc

    # 在同一事务内分配严格递增 seq 并追加 WorkflowEvent
    def append(
        self,
        workflow_id: str,
        kind: str,
        *,
        node_id: str = "",
        node_kind: GraphNodeKind | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> WorkflowEvent:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
            if row is None:
                raise WorkflowLedgerError(f"workflow not found: {workflow_id}")
            seq_row = connection.execute(
                "SELECT next_seq FROM workflow_event_counters WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            if seq_row is None:
                raise WorkflowLedgerError(
                    f"workflow event counter not found: {workflow_id}"
                )
            next_seq = int(seq_row["next_seq"])
            connection.execute(
                "UPDATE workflow_event_counters SET next_seq = ? WHERE workflow_id = ?",
                (next_seq + 1, workflow_id),
            )
            event = WorkflowEvent.model_validate(
                {
                    "workflow_id": workflow_id,
                    "seq": next_seq,
                    "kind": kind,
                    "node_id": node_id,
                    "node_kind": node_kind,
                    "details": details or {},
                    "at": _now(),
                }
            )
            connection.execute(
                """
                INSERT INTO workflow_events(
                    workflow_id, seq, kind, node_id, node_kind, details_json, at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.workflow_id,
                    event.seq,
                    event.kind,
                    event.node_id,
                    event.node_kind.value if event.node_kind else None,
                    json.dumps(event.details, ensure_ascii=False, sort_keys=True),
                    event.at,
                ),
            )
            status = {
                "workflow.started": "running",
                "workflow.completed": "completed",
                "workflow.failed": "failed",
                "workflow.interrupted": "interrupted",
            }.get(event.kind)
            if status is not None:
                receipt = event.details.get("receipt")
                receipt_json = (
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True)
                    if isinstance(receipt, dict)
                    else "{}"
                )
                connection.execute(
                    "UPDATE workflows SET status = ?, receipt_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (status, receipt_json, event.at, workflow_id),
                )
            else:
                connection.execute(
                    "UPDATE workflows SET updated_at = ? WHERE id = ?",
                    (event.at, workflow_id),
                )
            return event

    # 按 seq 读取全部 durable WorkflowEvent
    def events(self, workflow_id: str) -> list[WorkflowEvent]:
        return self.events_after(workflow_id, 0)

    # 读取 seq 大于游标的 durable WorkflowEvent（workflow 不存在时报错）
    def events_after(self, workflow_id: str, after_seq: int) -> list[WorkflowEvent]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM workflow_events WHERE workflow_id = ? AND seq > ? "
                "ORDER BY seq",
                (workflow_id, after_seq),
            ).fetchall()
        if not rows and not self.exists(workflow_id):
            raise WorkflowLedgerError(f"workflow not found: {workflow_id}")
        try:
            return [
                WorkflowEvent.model_validate(
                    {
                        "workflow_id": row["workflow_id"],
                        "seq": row["seq"],
                        "kind": row["kind"],
                        "node_id": row["node_id"],
                        "node_kind": row["node_kind"],
                        "details": json.loads(row["details_json"]),
                        "at": row["at"],
                    }
                )
                for row in rows
            ]
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise WorkflowLedgerError(
                f"invalid workflow event stream {workflow_id}: {exc}"
            ) from exc

    # 判断 workflow 定义是否存在
    def exists(self, workflow_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM workflows WHERE id = ?",
                (workflow_id,),
            ).fetchone()
        return row is not None

    # 从 ledger 事件重建当前 Work Graph；基于缓存增量重放，避免每次全量 reduce
    def graph(self, workflow_id: str) -> WorkGraphState:
        with self._lock:
            cached = self._graph_cache.get(workflow_id)
            after_seq = cached[0] if cached is not None else 0
            new_events = self.events_after(workflow_id, after_seq)
            if cached is None:
                state = reduce_workflow_events(workflow_id, new_events)
                last_seq = new_events[-1].seq if new_events else 0
            else:
                state = cached[1]
                for event in new_events:
                    state = apply_workflow_event(state, event)
                last_seq = new_events[-1].seq if new_events else cached[0]
            self._graph_cache[workflow_id] = (last_seq, state)
            return state

    # 将 crash 遗留的 running 节点和 workflow 标记为 interrupted，完成节点保持不变
    def recover_interrupted(self, workflow_id: str) -> WorkGraphState:
        state = self.graph(workflow_id)
        if state.status != "running":
            return state
        for node in state.nodes.values():
            if node.status.value == "running":
                self.append(
                    workflow_id,
                    "node.interrupted",
                    node_id=node.id,
                    details={"reason": "core_restarted"},
                )
        self.append(
            workflow_id,
            "workflow.interrupted",
            details={"reason": "core_restarted"},
        )
        return self.graph(workflow_id)

    # 稳定列出 workflow 元数据，不加载完整事件正文
    def list(self) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, status, created_at, updated_at FROM workflows "
                "ORDER BY created_at, id"
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
        ]

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import JsonValue

from code_rook.core.authority import SandboxCapability, ToolAction
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.runtime.migrations import (
    CURRENT_SCHEMA_VERSION,
    connect_database,
    migrate_database,
    open_database,
)
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    SessionFacadeRecord,
    ThreadRecord,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)

_TERMINAL_TURN_STATUSES = {
    TurnStatus.COMPLETED,
    TurnStatus.FAILED,
    TurnStatus.INTERRUPTED,
}
_TURN_TRANSITIONS = {
    TurnStatus.QUEUED: {
        TurnStatus.RUNNING,
        TurnStatus.FAILED,
        TurnStatus.INTERRUPTED,
    },
    TurnStatus.RUNNING: {
        TurnStatus.WAITING,
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.INTERRUPTED,
    },
    TurnStatus.WAITING: {
        TurnStatus.RUNNING,
        TurnStatus.COMPLETED,
        TurnStatus.FAILED,
        TurnStatus.INTERRUPTED,
    },
    TurnStatus.COMPLETED: set(),
    TurnStatus.FAILED: set(),
    TurnStatus.INTERRUPTED: set(),
}


class RuntimeStoreError(RuntimeError):
    pass


class RecordNotFoundError(RuntimeStoreError):
    pass


class RecordAlreadyExistsError(RuntimeStoreError):
    pass


class InvalidTurnTransitionError(RuntimeStoreError):
    pass


class DuplicateTerminalResultError(RuntimeStoreError):
    pass


class ToolCallNotFoundError(RuntimeStoreError):
    pass


class IncompleteToolCallError(RuntimeStoreError):
    pass


# 将 JSON 值编码为稳定的紧凑文本
def _dump_json(value: JsonValue | dict[str, JsonValue] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


# 将 SQLite JSON 文本解码为字典
def _load_json_dict(value: str | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise RuntimeStoreError("stored JSON value is not an object")
    return loaded


# 将 JSON 数组文本解码为字符串列表
def _load_json_list(value: str) -> list[str]:
    loaded = json.loads(value)
    if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
        raise RuntimeStoreError("stored JSON value is not a string array")
    return loaded


# 将 datetime 编码为 ISO 8601 文本
def _dump_datetime(value: datetime) -> str:
    return value.isoformat()


# 将 ISO 8601 文本解码为 datetime
def _load_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


# 将数据库行还原为 ThreadRecord
def _thread_from_row(row: sqlite3.Row) -> ThreadRecord:
    return ThreadRecord(
        id=row["id"],
        title=row["title"],
        workspace=row["workspace"],
        status=row["status"],
        default_route_id=row["default_route_id"],
        created_at=_load_datetime(row["created_at"]),
        updated_at=_load_datetime(row["updated_at"]),
        schema_version=row["schema_version"],
    )


# 将数据库行还原为 TurnRecord
def _turn_from_row(row: sqlite3.Row) -> TurnRecord:
    return TurnRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        status=row["status"],
        mode=row["mode"],
        authority_profile=row["authority_profile"],
        workspace_trust=row["workspace_trust"],
        sandbox=SandboxCapability.model_validate(_load_json_dict(row["sandbox_json"])),
        allowed_actions=frozenset(
            ToolAction(action)
            for action in _load_json_list(row["allowed_actions_json"])
        ),
        route=(
            RouteReceipt.model_validate(_load_json_dict(row["route_json"]))
            if row["route_json"] is not None
            else None
        ),
        usage=_load_json_dict(row["usage_json"]) or {},
        error=_load_json_dict(row["error_json"]),
        boot_id=row["boot_id"],
        created_at=_load_datetime(row["created_at"]),
        updated_at=_load_datetime(row["updated_at"]),
        schema_version=row["schema_version"],
    )


# 将数据库行还原为 TurnItemRecord
def _item_from_row(row: sqlite3.Row) -> TurnItemRecord:
    return TurnItemRecord(
        id=row["id"],
        turn_id=row["turn_id"],
        kind=row["kind"],
        payload=_load_json_dict(row["payload_json"]) or {},
        tool_call_id=row["tool_call_id"],
        created_at=_load_datetime(row["created_at"]),
        schema_version=row["schema_version"],
    )


# 将数据库行还原为 RuntimeEventRecord
def _event_from_row(row: sqlite3.Row) -> RuntimeEventRecord:
    return RuntimeEventRecord(
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        seq=row["seq"],
        type=row["type"],
        payload=_load_json_dict(row["payload_json"]) or {},
        ts=_load_datetime(row["ts"]),
        schema_version=row["schema_version"],
    )


class RuntimeStore:
    # 初始化并迁移 runtime SQLite 数据库
    def __init__(self, path: Path) -> None:
        self.path = path
        migrate_database(path)

    # 返回当前 runtime schema 版本
    def schema_version(self) -> int:
        with connect_database(self.path) as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    # 创建 thread 并初始化其事件序号
    def create_thread(self, record: ThreadRecord) -> None:
        try:
            with connect_database(self.path) as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_threads (
                        id, title, workspace, status, default_route_id,
                        created_at, updated_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.title,
                        record.workspace,
                        record.status.value,
                        record.default_route_id,
                        _dump_datetime(record.created_at),
                        _dump_datetime(record.updated_at),
                        record.schema_version,
                    ),
                )
                connection.execute(
                    "INSERT INTO runtime_event_counters (thread_id, next_seq) VALUES (?, 1)",
                    (record.id,),
                )
        except sqlite3.IntegrityError as exc:
            raise RecordAlreadyExistsError(f"thread already exists: {record.id}") from exc

    # 新增或覆盖 thread 投影并确保事件计数器存在
    def upsert_thread(self, record: ThreadRecord) -> None:
        with connect_database(self.path) as connection:
            connection.execute(
                """
                INSERT INTO runtime_threads (
                    id, title, workspace, status, default_route_id,
                    created_at, updated_at, schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    workspace = excluded.workspace,
                    status = excluded.status,
                    default_route_id = excluded.default_route_id,
                    updated_at = excluded.updated_at,
                    schema_version = excluded.schema_version
                """,
                (
                    record.id,
                    record.title,
                    record.workspace,
                    record.status.value,
                    record.default_route_id,
                    _dump_datetime(record.created_at),
                    _dump_datetime(record.updated_at),
                    record.schema_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO runtime_event_counters (thread_id, next_seq)
                VALUES (?, 1)
                ON CONFLICT(thread_id) DO NOTHING
                """,
                (record.id,),
            )

    # 按 id 查询 thread
    def get_thread(self, thread_id: str) -> ThreadRecord:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM runtime_threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        return _thread_from_row(row)

    # 按最近更新时间倒序列出全部 thread
    def list_threads(self) -> list[ThreadRecord]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_threads ORDER BY updated_at DESC, id"
            ).fetchall()
        return [_thread_from_row(row) for row in rows]

    # 删除 thread 并级联清理 turn、item、event 与 facade
    def delete_thread(self, thread_id: str) -> None:
        with connect_database(self.path) as connection:
            cursor = connection.execute(
                "DELETE FROM runtime_threads WHERE id = ?",
                (thread_id,),
            )
        if cursor.rowcount == 0:
            raise RecordNotFoundError(f"thread not found: {thread_id}")

    # 新增或覆盖 session 兼容 facade 元数据
    def upsert_session_facade(self, record: SessionFacadeRecord) -> None:
        try:
            with connect_database(self.path) as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_session_facades (
                        thread_id, mode, parent_thread_id
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        mode = excluded.mode,
                        parent_thread_id = excluded.parent_thread_id
                    """,
                    (record.thread_id, record.mode, record.parent_thread_id),
                )
        except sqlite3.IntegrityError as exc:
            raise RecordNotFoundError(f"thread not found: {record.thread_id}") from exc

    # 查询 session 兼容 facade 元数据
    def get_session_facade(self, thread_id: str) -> SessionFacadeRecord:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM runtime_session_facades WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"session facade not found: {thread_id}")
        return SessionFacadeRecord(
            thread_id=row["thread_id"],
            mode=row["mode"],
            parent_thread_id=row["parent_thread_id"],
        )

    # 创建属于现有 thread 的 turn
    def create_turn(self, record: TurnRecord) -> None:
        try:
            with connect_database(self.path) as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_turns (
                        id, thread_id, status, mode, authority_profile,
                        workspace_trust, sandbox_json, allowed_actions_json,
                        route_json, usage_json, error_json, boot_id,
                        created_at, updated_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.thread_id,
                        record.status.value,
                        record.mode.value,
                        record.authority_profile.value,
                        record.workspace_trust.value,
                        _dump_json(record.sandbox.model_dump()),
                        _dump_json(
                            [action.value for action in sorted(record.allowed_actions)]
                        ),
                        _dump_json(
                            record.route.model_dump(mode="json")
                            if record.route is not None
                            else None
                        ),
                        _dump_json(record.usage),
                        _dump_json(record.error),
                        record.boot_id,
                        _dump_datetime(record.created_at),
                        _dump_datetime(record.updated_at),
                        record.schema_version,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            if self._thread_exists(record.thread_id):
                raise RecordAlreadyExistsError(f"turn already exists: {record.id}") from exc
            raise RecordNotFoundError(f"thread not found: {record.thread_id}") from exc

    # 按 id 查询 turn
    def get_turn(self, turn_id: str) -> TurnRecord:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT * FROM runtime_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"turn not found: {turn_id}")
        return _turn_from_row(row)

    # 按创建时间列出 thread 的全部 turn
    def list_turns(self, thread_id: str) -> list[TurnRecord]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_turns
                WHERE thread_id = ?
                ORDER BY created_at, id
                """,
                (thread_id,),
            ).fetchall()
        return [_turn_from_row(row) for row in rows]

    # 在单一事务中写入 item、可选状态变化与对应事件
    def record_item_and_event(
        self,
        item: TurnItemRecord,
        *,
        event_type: str,
        event_payload: dict[str, JsonValue],
        event_ts: datetime,
        turn_status: TurnStatus | None = None,
    ) -> RuntimeEventRecord:
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            turn_row = connection.execute(
                "SELECT * FROM runtime_turns WHERE id = ?",
                (item.turn_id,),
            ).fetchone()
            if turn_row is None:
                raise RecordNotFoundError(f"turn not found: {item.turn_id}")
            current_status = TurnStatus(turn_row["status"])
            if current_status in _TERMINAL_TURN_STATUSES:
                raise InvalidTurnTransitionError(
                    f"cannot append item to terminal turn {item.turn_id}"
                )
            if turn_status is not None:
                self._validate_transition(current_status, turn_status)
            self._validate_tool_result(connection, item)
            self._insert_item(connection, item)
            if turn_status in _TERMINAL_TURN_STATUSES:
                self._validate_terminal_tool_pairs(connection, item.turn_id)
            if turn_status is not None and turn_status != current_status:
                connection.execute(
                    "UPDATE runtime_turns SET status = ?, updated_at = ? WHERE id = ?",
                    (turn_status.value, _dump_datetime(event_ts), item.turn_id),
                )
            event = self._insert_event(
                connection,
                thread_id=turn_row["thread_id"],
                turn_id=item.turn_id,
                event_type=event_type,
                payload=event_payload,
                ts=event_ts,
            )
            connection.commit()
            return event
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if (
                item.kind == TurnItemKind.TOOL_RESULT
                and "runtime_turn_items.turn_id, runtime_turn_items.tool_call_id"
                in str(exc)
            ):
                raise DuplicateTerminalResultError(
                    f"terminal result already exists: {item.tool_call_id}"
                ) from exc
            raise RecordAlreadyExistsError(f"item already exists: {item.id}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # 追加不含 item 的 runtime 事件并分配 thread 序号
    def append_event(
        self,
        *,
        thread_id: str,
        turn_id: str | None,
        event_type: str,
        payload: dict[str, JsonValue],
        ts: datetime,
    ) -> RuntimeEventRecord:
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            event = self._insert_event(
                connection,
                thread_id=thread_id,
                turn_id=turn_id,
                event_type=event_type,
                payload=payload,
                ts=ts,
            )
            connection.commit()
            return event
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise RecordNotFoundError(
                f"thread or turn not found: {thread_id}/{turn_id}"
            ) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # 在单一事务中改变 turn 状态并写入对应事件
    def transition_turn_and_event(
        self,
        turn_id: str,
        *,
        status: TurnStatus,
        event_type: str,
        event_payload: dict[str, JsonValue],
        event_ts: datetime,
        error: dict[str, JsonValue] | None = None,
    ) -> RuntimeEventRecord:
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            turn_row = connection.execute(
                "SELECT * FROM runtime_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn_row is None:
                raise RecordNotFoundError(f"turn not found: {turn_id}")
            current_status = TurnStatus(turn_row["status"])
            if current_status in _TERMINAL_TURN_STATUSES:
                raise InvalidTurnTransitionError(
                    f"turn is already terminal: {turn_id}/{current_status.value}"
                )
            self._validate_transition(current_status, status)
            if status in _TERMINAL_TURN_STATUSES:
                self._validate_terminal_tool_pairs(connection, turn_id)
            connection.execute(
                """
                UPDATE runtime_turns
                SET status = ?, error_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    _dump_json(error),
                    _dump_datetime(event_ts),
                    turn_id,
                ),
            )
            event = self._insert_event(
                connection,
                thread_id=turn_row["thread_id"],
                turn_id=turn_id,
                event_type=event_type,
                payload=event_payload,
                ts=event_ts,
            )
            connection.commit()
            return event
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # 按创建顺序列出 turn 的全部 item
    def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_turn_items
                WHERE turn_id = ?
                ORDER BY created_at, id
                """,
                (turn_id,),
            ).fetchall()
        return [_item_from_row(row) for row in rows]

    # 返回指定序号之后的 thread 事件
    def list_events(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        up_to_seq: int | None = None,
        limit: int = 1000,
    ) -> list[RuntimeEventRecord]:
        if after_seq < 0:
            raise ValueError("after_seq must be non-negative")
        if limit < 1:
            raise ValueError("limit must be positive")
        if up_to_seq is not None and up_to_seq < after_seq:
            return []
        with connect_database(self.path) as connection:
            if up_to_seq is None:
                rows = connection.execute(
                    """
                    SELECT * FROM runtime_events
                    WHERE thread_id = ? AND seq > ?
                    ORDER BY seq
                    LIMIT ?
                    """,
                    (thread_id, after_seq, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM runtime_events
                    WHERE thread_id = ? AND seq > ? AND seq <= ?
                    ORDER BY seq
                    LIMIT ?
                    """,
                    (thread_id, after_seq, up_to_seq, limit),
                ).fetchall()
        return [_event_from_row(row) for row in rows]

    # 返回 thread 当前已提交的最大事件序号
    def latest_event_seq(self, thread_id: str) -> int:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT next_seq FROM runtime_event_counters WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        return int(row["next_seq"]) - 1

    # 中断其他 boot 留下的活动 turn，并为孤立工具调用补齐错误结果
    def recover_stale_turns(
        self,
        current_boot_id: str,
        ts: datetime,
    ) -> list[RuntimeEventRecord]:
        connection = open_database(self.path)
        events: list[RuntimeEventRecord] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM runtime_turns
                WHERE status IN ('running', 'waiting')
                  AND (boot_id IS NULL OR boot_id != ?)
                ORDER BY created_at, id
                """,
                (current_boot_id,),
            ).fetchall()
            for row in rows:
                turn_id = str(row["id"])
                for tool_call_id in self._unmatched_tool_call_ids(connection, turn_id):
                    self._insert_item(
                        connection,
                        TurnItemRecord(
                            id=f"{turn_id}:recovery:{tool_call_id}",
                            turn_id=turn_id,
                            kind=TurnItemKind.TOOL_RESULT,
                            tool_call_id=tool_call_id,
                            payload={
                                "status": "error",
                                "reason": "daemon_restarted",
                            },
                            created_at=ts,
                        ),
                    )
                error: dict[str, JsonValue] = {"reason": "daemon_restarted"}
                connection.execute(
                    """
                    UPDATE runtime_turns
                    SET status = 'interrupted', error_json = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (_dump_json(error), _dump_datetime(ts), turn_id),
                )
                connection.execute(
                    """
                    UPDATE runtime_threads
                    SET status = 'interrupted', updated_at = ?
                    WHERE id = ?
                    """,
                    (_dump_datetime(ts), row["thread_id"]),
                )
                events.append(
                    self._insert_event(
                        connection,
                        thread_id=row["thread_id"],
                        turn_id=turn_id,
                        event_type="turn.interrupted",
                        payload=error,
                        ts=ts,
                    )
                )
            connection.commit()
            return events
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # 检查 thread 是否存在
    def _thread_exists(self, thread_id: str) -> bool:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT 1 FROM runtime_threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        return row is not None

    # 校验 turn 状态变化符合显式状态机
    def _validate_transition(self, current: TurnStatus, target: TurnStatus) -> None:
        if target == current:
            return
        if target not in _TURN_TRANSITIONS[current]:
            raise InvalidTurnTransitionError(
                f"invalid turn transition: {current.value} -> {target.value}"
            )

    # 校验工具结果引用已有调用且尚无终态结果
    def _validate_tool_result(
        self,
        connection: sqlite3.Connection,
        item: TurnItemRecord,
    ) -> None:
        if item.kind != TurnItemKind.TOOL_RESULT:
            return
        call_row = connection.execute(
            """
            SELECT 1 FROM runtime_turn_items
            WHERE turn_id = ? AND kind = 'tool_call' AND tool_call_id = ?
            """,
            (item.turn_id, item.tool_call_id),
        ).fetchone()
        if call_row is None:
            raise ToolCallNotFoundError(f"tool call not found: {item.tool_call_id}")
        result_row = connection.execute(
            """
            SELECT 1 FROM runtime_turn_items
            WHERE turn_id = ? AND kind = 'tool_result' AND tool_call_id = ?
            """,
            (item.turn_id, item.tool_call_id),
        ).fetchone()
        if result_row is not None:
            raise DuplicateTerminalResultError(
                f"terminal result already exists: {item.tool_call_id}"
            )

    # 返回 turn 中尚未配对终态结果的工具调用标识
    def _unmatched_tool_call_ids(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT calls.tool_call_id
            FROM runtime_turn_items AS calls
            LEFT JOIN runtime_turn_items AS results
              ON results.turn_id = calls.turn_id
             AND results.kind = 'tool_result'
             AND results.tool_call_id = calls.tool_call_id
            WHERE calls.turn_id = ?
              AND calls.kind = 'tool_call'
              AND results.id IS NULL
            ORDER BY calls.created_at, calls.id
            """,
            (turn_id,),
        ).fetchall()
        return [str(row["tool_call_id"]) for row in rows]

    # 拒绝仍含孤立工具调用的 turn 进入终态
    def _validate_terminal_tool_pairs(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> None:
        unmatched = self._unmatched_tool_call_ids(connection, turn_id)
        if unmatched:
            raise IncompleteToolCallError(
                f"turn {turn_id} has incomplete tool calls: {', '.join(unmatched)}"
            )

    # 插入 turn item
    def _insert_item(
        self,
        connection: sqlite3.Connection,
        item: TurnItemRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO runtime_turn_items (
                id, turn_id, kind, payload_json, tool_call_id,
                created_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.turn_id,
                item.kind.value,
                _dump_json(item.payload),
                item.tool_call_id,
                _dump_datetime(item.created_at),
                item.schema_version,
            ),
        )

    # 分配下一个 thread seq 并插入事件
    def _insert_event(
        self,
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        turn_id: str | None,
        event_type: str,
        payload: dict[str, JsonValue],
        ts: datetime,
    ) -> RuntimeEventRecord:
        counter = connection.execute(
            "SELECT next_seq FROM runtime_event_counters WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if counter is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        if turn_id is not None:
            turn_row = connection.execute(
                "SELECT thread_id FROM runtime_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn_row is None:
                raise RecordNotFoundError(f"turn not found: {turn_id}")
            if turn_row["thread_id"] != thread_id:
                raise RuntimeStoreError(
                    f"turn {turn_id} does not belong to thread {thread_id}"
                )
        seq = int(counter["next_seq"])
        connection.execute(
            "UPDATE runtime_event_counters SET next_seq = ? WHERE thread_id = ?",
            (seq + 1, thread_id),
        )
        event = RuntimeEventRecord(
            thread_id=thread_id,
            turn_id=turn_id,
            seq=seq,
            type=event_type,
            payload=payload,
            ts=ts,
            schema_version=CURRENT_SCHEMA_VERSION,
        )
        connection.execute(
            """
            INSERT INTO runtime_events (
                thread_id, turn_id, seq, type, payload_json, ts, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.thread_id,
                event.turn_id,
                event.seq,
                event.type,
                _dump_json(event.payload),
                _dump_datetime(event.ts),
                event.schema_version,
            ),
        )
        return event

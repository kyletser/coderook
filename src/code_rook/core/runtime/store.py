from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from code_rook.core.artifacts import ImageArtifactInput
from code_rook.core.authority import SandboxCapability, ToolAction
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.runtime.migrations import (
    connect_database,
    migrate_database,
    open_database,
)
from code_rook.core.runtime.models import (
    RUNTIME_RECORD_SCHEMA_VERSION,
    QueuedMessageRecord,
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
logger = logging.getLogger(__name__)
_IMAGE_ATTACHMENTS = TypeAdapter(list[ImageArtifactInput])


class RuntimeStoreError(RuntimeError):
    pass


class UnsupportedRuntimeRecordSchemaError(RuntimeStoreError):
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


# 校验持久化行版本恰为当前版本并单独标识未来 schema
def _require_current_record_schema(value: object, record_kind: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeStoreError(f"invalid {record_kind} record schema version")
    if value > RUNTIME_RECORD_SCHEMA_VERSION:
        raise UnsupportedRuntimeRecordSchemaError(
            f"{record_kind} record schema {value} is newer than supported "
            f"{RUNTIME_RECORD_SCHEMA_VERSION}"
        )
    if value != RUNTIME_RECORD_SCHEMA_VERSION:
        raise RuntimeStoreError(f"invalid {record_kind} record schema version: {value}")


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
    _require_current_record_schema(row["schema_version"], "thread")
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
    _require_current_record_schema(row["schema_version"], "turn")
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
    _require_current_record_schema(row["schema_version"], "turn item")
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
    _require_current_record_schema(row["schema_version"], "runtime event")
    return RuntimeEventRecord(
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        seq=row["seq"],
        type=row["type"],
        payload=_load_json_dict(row["payload_json"]) or {},
        ts=_load_datetime(row["ts"]),
        schema_version=row["schema_version"],
    )


# 将数据库行还原为跨前端共享的排队消息
def _queued_message_from_row(row: sqlite3.Row) -> QueuedMessageRecord:
    _require_current_record_schema(row["schema_version"], "queued message")
    attachments = _IMAGE_ATTACHMENTS.validate_python(json.loads(row["attachments_json"]))
    return QueuedMessageRecord(
        id=row["id"],
        thread_id=row["thread_id"],
        content=row["content"],
        display_content=row["display_content"],
        mode=row["mode"],
        attachments=attachments,
        status=row["status"],
        error=row["error"],
        created_at=_load_datetime(row["created_at"]),
        updated_at=_load_datetime(row["updated_at"]),
        schema_version=row["schema_version"],
    )


class RuntimeStore:
    # 初始化并迁移 runtime SQLite 数据库
    def __init__(self, path: Path, *, migrate: bool = True) -> None:
        self.path = path
        if migrate:
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
            existing = connection.execute(
                "SELECT schema_version FROM runtime_threads WHERE id = ?",
                (record.id,),
            ).fetchone()
            if existing is not None:
                _require_current_record_schema(existing["schema_version"], "thread")
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
        records: list[ThreadRecord] = []
        for row in rows:
            try:
                records.append(_thread_from_row(row))
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.error(
                    "skipping invalid runtime thread record; run `coderook doctor runtime`"
                )
        return records

    # 删除 thread 并级联清理 turn、item、event 与 facade
    def delete_thread(self, thread_id: str) -> None:
        with connect_database(self.path) as connection:
            self._require_thread_schema(connection, thread_id)
            cursor = connection.execute(
                "DELETE FROM runtime_threads WHERE id = ?",
                (thread_id,),
            )
        if cursor.rowcount == 0:
            raise RecordNotFoundError(f"thread not found: {thread_id}")

    # 持久化一条由 TUI 或 Web 提交的后续消息
    def enqueue_message(self, record: QueuedMessageRecord) -> None:
        try:
            with connect_database(self.path) as connection:
                self._require_thread_schema(connection, record.thread_id)
                connection.execute(
                    """
                    INSERT INTO runtime_message_queue (
                        id, thread_id, content, display_content, mode,
                        attachments_json, status, error, created_at, updated_at,
                        schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.thread_id,
                        record.content,
                        record.display_content,
                        record.mode.value,
                        json.dumps(
                            [item.model_dump(mode="json") for item in record.attachments],
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        record.status,
                        record.error,
                        _dump_datetime(record.created_at),
                        _dump_datetime(record.updated_at),
                        record.schema_version,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RecordAlreadyExistsError(
                f"queued message already exists: {record.id}"
            ) from exc

    # 按提交顺序列出指定 thread 的全部待处理消息
    def list_queued_messages(self, thread_id: str) -> list[QueuedMessageRecord]:
        with connect_database(self.path) as connection:
            self._require_thread_schema(connection, thread_id)
            rows = connection.execute(
                """
                SELECT * FROM runtime_message_queue
                WHERE thread_id = ?
                ORDER BY created_at, id
                """,
                (thread_id,),
            ).fetchall()
        return [_queued_message_from_row(row) for row in rows]

    # 原子领取指定 thread 最早的一条可执行消息
    def claim_next_queued_message(
        self,
        thread_id: str,
        ts: datetime,
    ) -> QueuedMessageRecord | None:
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._require_thread_schema(connection, thread_id)
            row = connection.execute(
                """
                SELECT * FROM runtime_message_queue
                WHERE thread_id = ? AND status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            _require_current_record_schema(row["schema_version"], "queued message")
            connection.execute(
                """
                UPDATE runtime_message_queue
                SET status = 'dispatching', error = '', updated_at = ?
                WHERE id = ?
                """,
                (_dump_datetime(ts), row["id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM runtime_message_queue WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if claimed is None:
                raise RuntimeStoreError("claimed queued message disappeared")
            connection.commit()
            return _queued_message_from_row(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # 将领取失败的消息保留为可见 blocked 状态
    def block_queued_message(self, message_id: str, error: str, ts: datetime) -> None:
        with connect_database(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_message_queue
                SET status = 'blocked', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (error, _dump_datetime(ts), message_id),
            )
        if cursor.rowcount == 0:
            raise RecordNotFoundError(f"queued message not found: {message_id}")

    # 将用户确认重试的 blocked 消息重新放回可领取状态
    def retry_queued_message(
        self,
        thread_id: str,
        message_id: str,
        ts: datetime,
    ) -> None:
        with connect_database(self.path) as connection:
            self._require_thread_schema(connection, thread_id)
            cursor = connection.execute(
                """
                UPDATE runtime_message_queue
                SET status = 'queued', error = '', updated_at = ?
                WHERE thread_id = ? AND id = ? AND status = 'blocked'
                """,
                (_dump_datetime(ts), thread_id, message_id),
            )
        if cursor.rowcount == 0:
            raise RecordNotFoundError(
                f"blocked queued message not found: {message_id}"
            )

    # 删除已经完成或由用户取消的排队消息
    def delete_queued_message(self, thread_id: str, message_id: str) -> None:
        with connect_database(self.path) as connection:
            self._require_thread_schema(connection, thread_id)
            cursor = connection.execute(
                "DELETE FROM runtime_message_queue WHERE thread_id = ? AND id = ?",
                (thread_id, message_id),
            )
        if cursor.rowcount == 0:
            raise RecordNotFoundError(f"queued message not found: {message_id}")

    # daemon 重启时把执行结果不确定的领取记录转为需用户确认的 blocked 状态
    def recover_queued_messages(self, ts: datetime) -> int:
        with connect_database(self.path) as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_message_queue
                SET status = 'blocked',
                    error = 'daemon restarted while this message was dispatching',
                    updated_at = ?
                WHERE status = 'dispatching'
                """,
                (_dump_datetime(ts),),
            )
        return max(0, cursor.rowcount)

    # 新增或覆盖 session 兼容 facade 元数据
    def upsert_session_facade(self, record: SessionFacadeRecord) -> None:
        try:
            with connect_database(self.path) as connection:
                self._require_thread_schema(connection, record.thread_id)
                existing = connection.execute(
                    "SELECT schema_version FROM runtime_session_facades "
                    "WHERE thread_id = ?",
                    (record.thread_id,),
                ).fetchone()
                if existing is not None:
                    _require_current_record_schema(
                        existing["schema_version"],
                        "session facade",
                    )
                connection.execute(
                    """
                    INSERT INTO runtime_session_facades (
                        thread_id, mode, parent_thread_id, schema_version
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        mode = excluded.mode,
                        parent_thread_id = excluded.parent_thread_id,
                        schema_version = excluded.schema_version
                    """,
                    (
                        record.thread_id,
                        record.mode,
                        record.parent_thread_id,
                        record.schema_version,
                    ),
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
        _require_current_record_schema(row["schema_version"], "session facade")
        return SessionFacadeRecord(
            thread_id=row["thread_id"],
            mode=row["mode"],
            parent_thread_id=row["parent_thread_id"],
            schema_version=row["schema_version"],
        )

    # 创建属于现有 thread 的 turn
    def create_turn(self, record: TurnRecord) -> None:
        try:
            with connect_database(self.path) as connection:
                self._require_thread_schema(connection, record.thread_id)
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
        records: list[TurnRecord] = []
        for row in rows:
            try:
                records.append(_turn_from_row(row))
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                logger.error(
                    "skipping invalid runtime turn record; run `coderook doctor runtime`"
                )
        return records

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
            _require_current_record_schema(turn_row["schema_version"], "turn")
            self._require_turn_item_schemas(connection, item.turn_id)
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
            _require_current_record_schema(turn_row["schema_version"], "turn")
            self._require_turn_item_schemas(connection, turn_id)
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

    # 按 durable seq 列出单个 turn 的全部事件，不受 SSE 分页窗口限制
    def list_turn_events(self, turn_id: str) -> list[RuntimeEventRecord]:
        with connect_database(self.path) as connection:
            rows = connection.execute(
                """
                SELECT * FROM runtime_events
                WHERE turn_id = ?
                ORDER BY seq
                """,
                (turn_id,),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    # 返回 thread 当前已提交的最大事件序号
    def latest_event_seq(self, thread_id: str) -> int:
        with connect_database(self.path) as connection:
            self._require_thread_schema(connection, thread_id)
            self._require_thread_event_schemas(connection, thread_id)
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
                _require_current_record_schema(row["schema_version"], "turn")
                self._require_thread_schema(connection, str(row["thread_id"]))
                self._require_turn_item_schemas(connection, str(row["id"]))
            for thread_id in sorted({str(row["thread_id"]) for row in rows}):
                self._repair_recovery_counter(connection, thread_id)
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

    # 仅在事件序列完整时补齐恢复流程所需 counter，拒绝掩盖真实事件 gap
    def _repair_recovery_counter(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
    ) -> None:
        self._require_thread_event_schemas(connection, thread_id)
        sequence = connection.execute(
            """
            SELECT COUNT(*) AS event_count, MIN(seq) AS min_seq, MAX(seq) AS max_seq
            FROM runtime_events
            WHERE thread_id = ?
            """,
            (thread_id,),
        ).fetchone()
        event_count = int(sequence["event_count"])
        minimum = int(sequence["min_seq"]) if sequence["min_seq"] is not None else None
        maximum = int(sequence["max_seq"]) if sequence["max_seq"] is not None else 0
        if event_count and (minimum != 1 or maximum != event_count):
            raise RuntimeStoreError(
                "runtime event sequence is not contiguous; run coderook doctor runtime"
            )
        expected_next = maximum + 1
        connection.execute(
            """
            INSERT INTO runtime_event_counters (thread_id, next_seq)
            VALUES (?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET next_seq = excluded.next_seq
            """,
            (thread_id, expected_next),
        )

    # 在单一事务中更新 turn 用量并写入对应事件
    def update_usage_and_event(
        self,
        turn_id: str,
        *,
        usage: dict[str, JsonValue],
        event_type: str,
        event_payload: dict[str, JsonValue],
        event_ts: datetime,
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
            _require_current_record_schema(turn_row["schema_version"], "turn")
            connection.execute(
                "UPDATE runtime_turns SET usage_json = ?, updated_at = ? WHERE id = ?",
                (_dump_json(usage), _dump_datetime(event_ts), turn_id),
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

    # 检查 thread 是否存在
    def _thread_exists(self, thread_id: str) -> bool:
        with connect_database(self.path) as connection:
            row = connection.execute(
                "SELECT 1 FROM runtime_threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
        return row is not None

    # 要求目标 thread 存在且逐行 schema 为当前版本
    def _require_thread_schema(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT schema_version FROM runtime_threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        _require_current_record_schema(row["schema_version"], "thread")

    # 拒绝在含未来或损坏 item schema 的 turn 上继续推导或写入
    def _require_turn_item_schemas(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT schema_version FROM runtime_turn_items "
            "WHERE turn_id = ? AND schema_version != ? LIMIT 1",
            (turn_id, RUNTIME_RECORD_SCHEMA_VERSION),
        ).fetchone()
        if row is not None:
            _require_current_record_schema(row["schema_version"], "turn item")

    # 拒绝在含未来或损坏事件 schema 的 thread 上继续分配序号
    def _require_thread_event_schemas(
        self,
        connection: sqlite3.Connection,
        thread_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT schema_version FROM runtime_events "
            "WHERE thread_id = ? AND schema_version != ? LIMIT 1",
            (thread_id, RUNTIME_RECORD_SCHEMA_VERSION),
        ).fetchone()
        if row is not None:
            _require_current_record_schema(row["schema_version"], "runtime event")

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
            SELECT schema_version FROM runtime_turn_items
            WHERE turn_id = ? AND kind = 'tool_call' AND tool_call_id = ?
            """,
            (item.turn_id, item.tool_call_id),
        ).fetchone()
        if call_row is None:
            raise ToolCallNotFoundError(f"tool call not found: {item.tool_call_id}")
        _require_current_record_schema(call_row["schema_version"], "turn item")
        result_row = connection.execute(
            """
            SELECT schema_version FROM runtime_turn_items
            WHERE turn_id = ? AND kind = 'tool_result' AND tool_call_id = ?
            """,
            (item.turn_id, item.tool_call_id),
        ).fetchone()
        if result_row is not None:
            _require_current_record_schema(result_row["schema_version"], "turn item")
            raise DuplicateTerminalResultError(
                f"terminal result already exists: {item.tool_call_id}"
            )

    # 返回 turn 中尚未配对终态结果的工具调用标识
    def _unmatched_tool_call_ids(
        self,
        connection: sqlite3.Connection,
        turn_id: str,
    ) -> list[str]:
        self._require_turn_item_schemas(connection, turn_id)
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
            """
            SELECT counters.next_seq, threads.schema_version AS thread_schema_version
            FROM runtime_event_counters AS counters
            JOIN runtime_threads AS threads ON threads.id = counters.thread_id
            WHERE counters.thread_id = ?
            """,
            (thread_id,),
        ).fetchone()
        if counter is None:
            raise RecordNotFoundError(f"thread not found: {thread_id}")
        _require_current_record_schema(counter["thread_schema_version"], "thread")
        self._require_thread_event_schemas(connection, thread_id)
        if turn_id is not None:
            turn_row = connection.execute(
                "SELECT thread_id, schema_version FROM runtime_turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn_row is None:
                raise RecordNotFoundError(f"turn not found: {turn_id}")
            _require_current_record_schema(turn_row["schema_version"], "turn")
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

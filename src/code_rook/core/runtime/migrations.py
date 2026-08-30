from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CURRENT_SCHEMA_VERSION = 6


class RuntimeMigrationError(RuntimeError):
    pass


# 创建启用外键、WAL 和并发等待的 SQLite 连接
def open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextmanager
# 提供提交或回滚后必定关闭的 SQLite 连接上下文
def connect_database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_database(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


# 创建第一版 runtime 数据表与约束
def _apply_v1(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_threads (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            workspace TEXT NOT NULL,
            status TEXT NOT NULL,
            default_route_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runtime_event_counters (
            thread_id TEXT PRIMARY KEY
                REFERENCES runtime_threads(id) ON DELETE CASCADE,
            next_seq INTEGER NOT NULL CHECK (next_seq >= 1)
        );

        CREATE TABLE IF NOT EXISTS runtime_turns (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL
                REFERENCES runtime_threads(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            mode TEXT NOT NULL,
            authority_profile TEXT NOT NULL,
            route_json TEXT,
            usage_json TEXT NOT NULL,
            error_json TEXT,
            boot_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS runtime_turns_thread_id
            ON runtime_turns(thread_id, created_at);

        CREATE TABLE IF NOT EXISTS runtime_turn_items (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL
                REFERENCES runtime_turns(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            tool_call_id TEXT,
            created_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS runtime_turn_items_turn_id
            ON runtime_turn_items(turn_id, created_at);

        CREATE UNIQUE INDEX IF NOT EXISTS runtime_unique_tool_result
            ON runtime_turn_items(turn_id, tool_call_id)
            WHERE kind = 'tool_result';

        CREATE TABLE IF NOT EXISTS runtime_events (
            thread_id TEXT NOT NULL
                REFERENCES runtime_threads(id) ON DELETE CASCADE,
            turn_id TEXT
                REFERENCES runtime_turns(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            ts TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            PRIMARY KEY (thread_id, seq)
        );

        CREATE INDEX IF NOT EXISTS runtime_events_turn_id
            ON runtime_events(turn_id, seq);
        """
    )


# 增加 session 兼容 facade 元数据表
def _apply_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_session_facades (
            thread_id TEXT PRIMARY KEY
                REFERENCES runtime_threads(id) ON DELETE CASCADE,
            mode TEXT NOT NULL,
            parent_thread_id TEXT
        )
        """
    )


# 返回指定表当前的列名集合，供可重入迁移判断部分完成状态
def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


# 仅在列不存在时执行固定迁移 SQL，使崩溃后的同版本重试保持幂等
def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    if column in _column_names(connection, table):
        return
    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


# 为 turn 幂等增加冻结的 trust、sandbox 与 action scope
def _apply_v3(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "runtime_turns",
        "workspace_trust",
        "TEXT NOT NULL DEFAULT 'untrusted'",
    )
    _add_column_if_missing(
        connection,
        "runtime_turns",
        "sandbox_json",
        "TEXT NOT NULL DEFAULT "
        "'{\"available\":false,\"kind\":\"none\",\"reason\":\"legacy turn\"}'",
    )
    _add_column_if_missing(
        connection,
        "runtime_turns",
        "allowed_actions_json",
        "TEXT NOT NULL DEFAULT '[\"read\",\"mutate\",\"shell\",\"external\"]'",
    )


# 为 session facade 幂等增加逐行 schema 版本以阻断未来记录被旧版覆盖
def _apply_v4(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "runtime_session_facades",
        "schema_version",
        "INTEGER NOT NULL DEFAULT 1",
    )


# 修复早期 Web 候选版误把兼容 runtime event 行标记为 schema 3 的历史数据
def _apply_v5(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE runtime_events SET schema_version = 1 WHERE schema_version = 3"
    )


# 增加跨 TUI/Web 共用的持久消息队列
def _apply_v6(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runtime_message_queue (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL
                REFERENCES runtime_threads(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            display_content TEXT NOT NULL,
            mode TEXT NOT NULL,
            attachments_json TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS runtime_message_queue_thread
            ON runtime_message_queue(thread_id, created_at, id);
        """
    )


# 将 runtime 数据库迁移到当前 schema 版本
def migrate_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > CURRENT_SCHEMA_VERSION:
            raise RuntimeMigrationError(
                f"runtime database version {version} is newer than supported "
                f"version {CURRENT_SCHEMA_VERSION}"
            )
        if version == 0:
            _apply_v1(connection)
            connection.execute("PRAGMA user_version = 1")
            version = 1
        if version == 1:
            _apply_v2(connection)
            connection.execute("PRAGMA user_version = 2")
            version = 2
        if version == 2:
            _apply_v3(connection)
            connection.execute("PRAGMA user_version = 3")
            version = 3
        if version == 3:
            _apply_v4(connection)
            connection.execute("PRAGMA user_version = 4")
            version = 4
        if version == 4:
            _apply_v5(connection)
            connection.execute("PRAGMA user_version = 5")
            version = 5
        if version == 5:
            _apply_v6(connection)
            connection.execute("PRAGMA user_version = 6")

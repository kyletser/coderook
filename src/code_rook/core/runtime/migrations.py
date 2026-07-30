from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

CURRENT_SCHEMA_VERSION = 3


class RuntimeMigrationError(RuntimeError):
    pass


# 创建启用外键和并发等待的 SQLite 连接
def open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
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


# 为 turn 增加冻结的 trust、sandbox 与 action scope
def _apply_v3(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        ALTER TABLE runtime_turns
            ADD COLUMN workspace_trust TEXT NOT NULL DEFAULT 'untrusted';
        ALTER TABLE runtime_turns
            ADD COLUMN sandbox_json TEXT NOT NULL
            DEFAULT '{"available":false,"kind":"none","reason":"legacy turn"}';
        ALTER TABLE runtime_turns
            ADD COLUMN allowed_actions_json TEXT NOT NULL
            DEFAULT '["read","mutate","shell","external"]';
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

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.goal.models import GoalRecord, UnsupportedGoalSchemaError
from code_rook.core.llm.credentials import inspect_credential_store
from code_rook.core.llm.migration_receipt import inspect_provider_catalog_migration
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.quarantine import (
    StateFileFingerprint,
    count_quarantined_records,
    fingerprint_state_file,
    quarantine_invalid_file,
)
from code_rook.core.runtime.migrations import (
    CURRENT_SCHEMA_VERSION,
    connect_database,
    migrate_database,
)
from code_rook.core.runtime.models import (
    RUNTIME_RECORD_SCHEMA_VERSION,
    SessionFacadeRecord,
    ThreadRecord,
    TurnRecord,
)
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import (
    RuntimeStore,
    RuntimeStoreError,
    _event_from_row,
    _item_from_row,
    _thread_from_row,
    _turn_from_row,
)
from code_rook.core.session.model import (
    SESSION_ID_PATTERN,
    Session,
    UnsupportedSessionSchemaError,
)
from code_rook.core.session.store import SessionStore
from code_rook.core.task.models import TaskRecord, UnsupportedTaskSchemaError
from code_rook.core.upgrade import inspect_v1_upgrade_backup

_TASK_FILE_RE = re.compile(r"^task_(\d+)\.json$")
_RUNTIME_SCHEMA_MANIFEST = {
    "runtime_threads": frozenset(
        {
            "id",
            "title",
            "workspace",
            "status",
            "default_route_id",
            "created_at",
            "updated_at",
            "schema_version",
        }
    ),
    "runtime_event_counters": frozenset({"thread_id", "next_seq"}),
    "runtime_turns": frozenset(
        {
            "id",
            "thread_id",
            "status",
            "mode",
            "authority_profile",
            "workspace_trust",
            "sandbox_json",
            "allowed_actions_json",
            "route_json",
            "usage_json",
            "error_json",
            "boot_id",
            "created_at",
            "updated_at",
            "schema_version",
        }
    ),
    "runtime_turn_items": frozenset(
        {
            "id",
            "turn_id",
            "kind",
            "payload_json",
            "tool_call_id",
            "created_at",
            "schema_version",
        }
    ),
    "runtime_events": frozenset(
        {
            "thread_id",
            "turn_id",
            "seq",
            "type",
            "payload_json",
            "ts",
            "schema_version",
        }
    ),
    "runtime_session_facades": frozenset(
        {"thread_id", "mode", "parent_thread_id", "schema_version"}
    ),
}
_REQUIRED_RUNTIME_TABLES = frozenset(_RUNTIME_SCHEMA_MANIFEST)
_REQUIRED_RUNTIME_INDEXES = frozenset(
    {
        "runtime_turns_thread_id",
        "runtime_turn_items_turn_id",
        "runtime_unique_tool_result",
        "runtime_events_turn_id",
    }
)
_REQUIRED_RUNTIME_FOREIGN_KEYS = {
    "runtime_event_counters": frozenset(
        {("thread_id", "runtime_threads", "id", "CASCADE")}
    ),
    "runtime_turns": frozenset({("thread_id", "runtime_threads", "id", "CASCADE")}),
    "runtime_turn_items": frozenset(
        {("turn_id", "runtime_turns", "id", "CASCADE")}
    ),
    "runtime_events": frozenset(
        {
            ("thread_id", "runtime_threads", "id", "CASCADE"),
            ("turn_id", "runtime_turns", "id", "CASCADE"),
        }
    ),
    "runtime_session_facades": frozenset(
        {("thread_id", "runtime_threads", "id", "CASCADE")}
    ),
}
_RUNTIME_REPAIR_BLOCKERS = frozenset(
    {
        "runtime_database_unreadable",
        "runtime_foreign_key_violation",
        "runtime_schema_incomplete",
        "runtime_schema_upgrade_required",
        "unsafe_runtime_database_path",
        "unsupported_runtime_schema",
        "unsupported_runtime_thread_schema",
        "unsupported_runtime_turn_schema",
        "unsupported_runtime_item_schema",
        "unsupported_runtime_event_schema",
        "unsupported_runtime_facade_schema",
    }
)


class RuntimeConsistencyIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Literal["warning", "error"]
    thread_id: str = ""
    turn_id: str = ""
    detail: str
    repairable: bool = False


class RuntimeReconcileReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    checked_at: datetime
    runtime_schema_version: int
    session_count: int = Field(ge=0)
    thread_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    quarantined_records: dict[str, int] = Field(default_factory=dict)
    migration_status: Literal["missing", "valid", "invalid"]
    backup_status: Literal["missing", "valid", "invalid"]
    provider_catalog_status: Literal["pending", "complete", "invalid"]
    credential_store_status: Literal["missing", "ready", "invalid"]
    route_catalog_status: Literal["missing", "ready", "degraded", "invalid"]
    healthy: bool
    issues: list[RuntimeConsistencyIssue] = Field(default_factory=list)
    repaired: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _CorruptStateRecord:
    path: Path
    category: Literal["session", "goal", "task"]
    fingerprint: StateFileFingerprint


@dataclass
class _StateSnapshot:
    sessions: list[Session] = field(default_factory=list)
    issues: list[RuntimeConsistencyIssue] = field(default_factory=list)
    corrupt_records: list[_CorruptStateRecord] = field(default_factory=list)


@dataclass
class _RuntimeSnapshot:
    schema_version: int = 0
    thread_count: int = 0
    turn_count: int = 0
    threads: list[ThreadRecord] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    tables: frozenset[str] = frozenset()
    issues: list[RuntimeConsistencyIssue] = field(default_factory=list)
    database_exists: bool = False


# 打开禁止写入且不会隐式创建数据库的 SQLite 连接
@contextmanager
def _read_only_database(path: Path) -> Iterator[sqlite3.Connection]:
    uri = f"{path.expanduser().resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        yield connection
    finally:
        connection.close()


# 把不可信持久化标识压缩为适合诊断输出的安全短字符串
def _safe_identifier(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^A-Za-z0-9._:-]", "?", value)[:80]


# 判断记录路径的任一父目录是否通过符号链接越出声明状态根
def _has_symlink_parent(path: Path, root: Path) -> bool:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError:
        return True
    current = root.absolute()
    if current.is_symlink():
        return True
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


# 生成不包含文件正文或绝对用户目录的状态记录标签
def _state_label(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return path.name


# 判断逐行 schema 是否来自当前程序尚不能解释的未来版本
def _is_future_runtime_record_schema(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > RUNTIME_RECORD_SCHEMA_VERSION
    )


class RuntimeReconciler:
    # 保存 runtime 投影、文件账本和 repair journal 的明确边界
    def __init__(
        self,
        runtime: RuntimeStore,
        sessions: SessionStore,
        *,
        workspace: Path,
        journal_path: Path,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._workspace = workspace
        self._journal_path = journal_path
        self._state_root = journal_path.parent.expanduser()

    # 对比文件账本、runtime 投影和事件序号且绝不移动或重写任何状态
    def inspect(self) -> RuntimeReconcileReport:
        repair_path_issues = self._inspect_repair_path_safety()
        if any(
            issue.code == "unsafe_repair_state_root" for issue in repair_path_issues
        ):
            return RuntimeReconcileReport(
                checked_at=datetime.now(UTC),
                runtime_schema_version=0,
                session_count=0,
                thread_count=0,
                turn_count=0,
                migration_status="invalid",
                backup_status="invalid",
                provider_catalog_status="invalid",
                credential_store_status="invalid",
                route_catalog_status="invalid",
                healthy=False,
                issues=repair_path_issues,
            )
        state = self._inspect_state_documents()
        runtime = self._inspect_runtime_records(has_sessions=bool(state.sessions))
        session_by_id = {session.id: session for session in state.sessions}
        thread_by_id = {thread.id: thread for thread in runtime.threads}
        turns_by_thread: dict[str, set[str]] = {}
        for turn in runtime.turns:
            turns_by_thread.setdefault(turn.thread_id, set()).add(turn.id)
        issues = [*repair_path_issues, *state.issues, *runtime.issues]
        runtime_semantics_available = (
            runtime.schema_version == CURRENT_SCHEMA_VERSION
            and _REQUIRED_RUNTIME_TABLES.issubset(runtime.tables)
            and not any(
                issue.code == "runtime_database_unreadable"
                for issue in runtime.issues
            )
        )

        for session in state.sessions:
            transcript = self._sessions.session_dir(session.id) / "thread.jsonl"
            if transcript.is_symlink():
                issues.append(
                    RuntimeConsistencyIssue(
                        code="unsafe_session_ledger",
                        severity="error",
                        thread_id=session.id,
                        detail="session transcript is a symbolic link",
                    )
                )
            else:
                try:
                    ledger_issues = self._sessions.verify_ledger(session.id)
                except (OSError, UnicodeError, ValueError):
                    ledger_issues = ["session transcript could not be read"]
                for detail in ledger_issues:
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="transcript_checksum_mismatch",
                            severity="error",
                            thread_id=session.id,
                            detail=detail,
                        )
                    )
            if not runtime_semantics_available:
                continue
            thread = thread_by_id.get(session.id)
            if thread is None:
                issues.append(
                    RuntimeConsistencyIssue(
                        code="missing_thread_projection",
                        severity="error",
                        thread_id=session.id,
                        detail="session ledger exists but runtime thread is missing",
                        repairable=True,
                    )
                )
                continue
            runtime_turns = turns_by_thread.get(session.id, set())
            for run_id in session.run_ids:
                if run_id not in runtime_turns:
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="missing_turn_projection",
                            severity="error",
                            thread_id=session.id,
                            turn_id=run_id,
                            detail="session run index exists but runtime turn is missing",
                            repairable=True,
                        )
                    )

        for thread in runtime.threads if runtime_semantics_available else []:
            if thread.id not in session_by_id:
                issues.append(
                    RuntimeConsistencyIssue(
                        code="orphan_runtime_thread",
                        severity="warning",
                        thread_id=thread.id,
                        detail="runtime thread has no compatible session ledger",
                    )
                )

        if runtime_semantics_available:
            issues.extend(self._inspect_event_sequences())
        quarantined_records = count_quarantined_records(self._state_root)
        for category, count in sorted(quarantined_records.items()):
            issues.append(
                RuntimeConsistencyIssue(
                    code="quarantined_state_records",
                    severity="warning",
                    detail=(
                        f"{count} invalid {category} record(s) are isolated; "
                        "inspect the quarantine journal before deleting them"
                    ),
                )
            )
        backup_status = inspect_v1_upgrade_backup(self._state_root)
        if backup_status != "valid":
            issues.append(
                RuntimeConsistencyIssue(
                    code="provider_catalog_migration_backup",
                    severity="error" if backup_status == "invalid" else "warning",
                    detail=f"v1 migration backup status is {backup_status}",
                )
            )
        provider_catalog_status = inspect_provider_catalog_migration(self._state_root)
        if provider_catalog_status == "invalid":
            issues.append(
                RuntimeConsistencyIssue(
                    code="provider_catalog_migration_receipt",
                    severity="error",
                    detail="v1 Provider Catalog migration receipt is invalid",
                )
            )
        credential_store_status = inspect_credential_store(
            self._state_root / "credentials.json"
        )
        if credential_store_status == "invalid":
            issues.append(
                RuntimeConsistencyIssue(
                    code="credential_store_invalid",
                    severity="error",
                    detail=(
                        "credential store is unreadable, unsafe, or uses an "
                        "unsupported schema"
                    ),
                )
            )
        route_path = self._state_root / "routes.json"
        if not os.path.lexists(route_path):
            route_catalog_status: Literal[
                "missing", "ready", "degraded", "invalid"
            ] = "missing"
        else:
            try:
                route_inspection = RouteStore(route_path).inspect()
            except (OSError, RouteStoreError, ValueError):
                route_catalog_status = "invalid"
                issues.append(
                    RuntimeConsistencyIssue(
                        code="provider_route_catalog_invalid",
                        severity="error",
                        detail=(
                            "provider route catalog is unreadable, unsafe, or uses "
                            "an unsupported schema"
                        ),
                    )
                )
            else:
                if route_inspection.issues:
                    route_catalog_status = "degraded"
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="provider_route_catalog_degraded",
                            severity=(
                                "error"
                                if route_inspection.active_route_unavailable
                                else "warning"
                            ),
                            detail=(
                                f"provider route catalog has "
                                f"{len(route_inspection.issues)} invalid record(s)"
                            ),
                        )
                    )
                else:
                    route_catalog_status = "ready"
        return RuntimeReconcileReport(
            checked_at=datetime.now(UTC),
            runtime_schema_version=runtime.schema_version,
            session_count=len(state.sessions),
            thread_count=runtime.thread_count,
            turn_count=runtime.turn_count,
            quarantined_records=quarantined_records,
            migration_status=backup_status,
            backup_status=backup_status,
            provider_catalog_status=provider_catalog_status,
            credential_store_status=credential_store_status,
            route_catalog_status=route_catalog_status,
            healthy=not any(issue.severity == "error" for issue in issues),
            issues=issues,
        )

    # 只读验证 repair 状态根和 journal 都是边界内真实路径
    def _inspect_repair_path_safety(self) -> list[RuntimeConsistencyIssue]:
        issues: list[RuntimeConsistencyIssue] = []
        state_root = self._state_root.absolute()
        journal = self._journal_path.absolute()
        if os.path.lexists(state_root) and (
            state_root.is_symlink() or not state_root.is_dir()
        ):
            issues.append(
                RuntimeConsistencyIssue(
                    code="unsafe_repair_state_root",
                    severity="error",
                    detail="runtime repair state root must be a real directory",
                )
            )
            return issues
        if journal.parent != state_root or (
            os.path.lexists(journal) and (journal.is_symlink() or not journal.is_file())
        ):
            issues.append(
                RuntimeConsistencyIssue(
                    code="unsafe_repair_journal_path",
                    severity="error",
                    detail="runtime repair journal must be a real file inside the state root",
                )
            )
        return issues

    # 只读扫描 Session、Goal 和 Task 文档并区分损坏记录与未来版本记录
    def _inspect_state_documents(self) -> _StateSnapshot:
        snapshot = _StateSnapshot()
        self._inspect_session_documents(snapshot)
        self._inspect_goal_documents(snapshot)
        self._inspect_task_documents(snapshot)
        snapshot.sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return snapshot

    # 记录单条坏状态且仅在可取得 no-follow 指纹时允许后续显式隔离
    def _record_corrupt_state(
        self,
        snapshot: _StateSnapshot,
        *,
        path: Path,
        category: Literal["session", "goal", "task"],
        code: str,
        thread_id: str = "",
    ) -> None:
        try:
            fingerprint = fingerprint_state_file(path)
        except (OSError, RuntimeError):
            fingerprint = None
        snapshot.issues.append(
            RuntimeConsistencyIssue(
                code=code,
                severity="error",
                thread_id=thread_id,
                detail=f"invalid state record: {_state_label(path, self._state_root)}",
                repairable=fingerprint is not None,
            )
        )
        if fingerprint is not None:
            snapshot.corrupt_records.append(
                _CorruptStateRecord(path, category, fingerprint)
            )

    # 只读校验全部 Session 元数据并保留可显式隔离的坏文件列表
    def _inspect_session_documents(self, snapshot: _StateSnapshot) -> None:
        root = self._sessions.root
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            snapshot.issues.append(
                RuntimeConsistencyIssue(
                    code="unsafe_session_state_root",
                    severity="error",
                    detail="session state root is not a real directory",
                )
            )
            return
        for path in sorted(root.glob("sess-*/meta.json")):
            sid = path.parent.name
            if _has_symlink_parent(path, root):
                snapshot.issues.append(
                    RuntimeConsistencyIssue(
                        code="unsafe_session_state_path",
                        severity="error",
                        thread_id=_safe_identifier(sid),
                        detail="session metadata has a symbolic-link parent",
                    )
                )
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("unsafe session metadata file")
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("session metadata must be an object")
                if SESSION_ID_PATTERN.fullmatch(sid) is None:
                    raise ValueError("invalid session directory identity")
                session = Session.from_dict(raw)
                if session.id != sid:
                    raise ValueError("session identity mismatch")
                snapshot.sessions.append(session)
            except UnsupportedSessionSchemaError:
                snapshot.issues.append(
                    RuntimeConsistencyIssue(
                        code="unsupported_session_schema",
                        severity="error",
                        thread_id=_safe_identifier(sid),
                        detail="session record requires a newer CodeRook version",
                    )
                )
            except (KeyError, OSError, TypeError, UnicodeError, ValueError):
                self._record_corrupt_state(
                    snapshot,
                    path=path,
                    category="session",
                    code="invalid_session_record",
                    thread_id=_safe_identifier(sid),
                )

    # 只读校验全部用户级 Goal 文档且不降级未来 schema
    def _inspect_goal_documents(self, snapshot: _StateSnapshot) -> None:
        root = self._state_root / "goals"
        if not root.exists():
            return
        if root.is_symlink() or not root.is_dir():
            snapshot.issues.append(
                RuntimeConsistencyIssue(
                    code="unsafe_goal_state_root",
                    severity="error",
                    detail="goal state root is not a real directory",
                )
            )
            return
        for path in sorted(root.glob("goal-*.json")):
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("unsafe goal file")
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("goal document must be an object")
                record = GoalRecord.from_dict(raw)
                if record.id != path.stem:
                    raise ValueError("goal identity mismatch")
            except UnsupportedGoalSchemaError:
                snapshot.issues.append(
                    RuntimeConsistencyIssue(
                        code="unsupported_goal_schema",
                        severity="error",
                        detail="goal record requires a newer CodeRook version",
                    )
                )
            except (KeyError, OSError, TypeError, UnicodeError, ValueError):
                self._record_corrupt_state(
                    snapshot,
                    path=path,
                    category="goal",
                    code="invalid_goal_record",
                )

    # 只读校验每个 run 的 Task 文档且拒绝遍历符号链接任务目录
    def _inspect_task_documents(self, snapshot: _StateSnapshot) -> None:
        sessions_root = self._state_root / "sessions"
        if not sessions_root.exists() or sessions_root.is_symlink():
            return
        task_roots = sorted(sessions_root.glob("sess-*/runs/*/.tasks"))
        for root in task_roots:
            if root.is_symlink() or _has_symlink_parent(root / "sentinel", sessions_root):
                snapshot.issues.append(
                    RuntimeConsistencyIssue(
                        code="unsafe_task_state_path",
                        severity="error",
                        detail="task state directory crosses a symbolic-link boundary",
                    )
                )
                continue
            for path in sorted(root.glob("task_*.json")):
                matched = _TASK_FILE_RE.fullmatch(path.name)
                try:
                    if matched is None or path.is_symlink() or not path.is_file():
                        raise ValueError("unsafe task file")
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("task document must be an object")
                    record = TaskRecord.from_dict(raw)
                    if record.id != int(matched.group(1)):
                        raise ValueError("task identity mismatch")
                except UnsupportedTaskSchemaError:
                    snapshot.issues.append(
                        RuntimeConsistencyIssue(
                            code="unsupported_task_schema",
                            severity="error",
                            detail="task record requires a newer CodeRook version",
                        )
                    )
                except (KeyError, OSError, TypeError, UnicodeError, ValueError):
                    self._record_corrupt_state(
                        snapshot,
                        path=path,
                        category="task",
                        code="invalid_task_record",
                    )

    # 逐行读取 runtime 核心记录，使单条坏行只形成诊断问题而不击穿 Doctor
    def _inspect_runtime_records(self, *, has_sessions: bool) -> _RuntimeSnapshot:
        runtime_path = self._runtime.path.expanduser().absolute()
        snapshot = _RuntimeSnapshot(database_exists=runtime_path.is_file())
        try:
            state_root = self._state_root.resolve(strict=True)
            parent = runtime_path.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            state_root = self._state_root.absolute()
            parent = runtime_path.parent.absolute()
        if runtime_path.is_symlink() or not parent.is_relative_to(state_root):
            snapshot.issues.append(
                RuntimeConsistencyIssue(
                    code="unsafe_runtime_database_path",
                    severity="error",
                    detail="runtime database must be a real file inside the state root",
                )
            )
            snapshot.database_exists = False
            return snapshot
        if not snapshot.database_exists:
            snapshot.issues.append(
                RuntimeConsistencyIssue(
                    code="runtime_database_missing",
                    severity="error" if has_sessions else "warning",
                    detail="runtime database does not exist",
                    repairable=has_sessions,
                )
            )
            return snapshot
        try:
            with _read_only_database(self._runtime.path) as connection:
                snapshot.schema_version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                table_rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                snapshot.tables = frozenset(str(row["name"]) for row in table_rows)
                if snapshot.schema_version > CURRENT_SCHEMA_VERSION:
                    snapshot.issues.append(
                        RuntimeConsistencyIssue(
                            code="unsupported_runtime_schema",
                            severity="error",
                            detail="runtime database requires a newer CodeRook version",
                        )
                    )
                    snapshot.tables = frozenset()
                    return snapshot
                if snapshot.schema_version < CURRENT_SCHEMA_VERSION:
                    snapshot.issues.append(
                        RuntimeConsistencyIssue(
                            code="runtime_schema_upgrade_required",
                            severity="error",
                            detail=(
                                f"runtime schema {snapshot.schema_version} must be "
                                f"upgraded to {CURRENT_SCHEMA_VERSION}"
                            ),
                            repairable=True,
                        )
                    )
                    snapshot.tables = frozenset()
                    return snapshot
                missing_tables = _REQUIRED_RUNTIME_TABLES - snapshot.tables
                if missing_tables:
                    snapshot.issues.append(
                        RuntimeConsistencyIssue(
                            code="runtime_schema_incomplete",
                            severity="error",
                            detail="runtime database is missing required tables",
                        )
                    )
                    snapshot.tables = frozenset()
                    return snapshot
                invalid_schema = False
                for table, expected_columns in _RUNTIME_SCHEMA_MANIFEST.items():
                    column_rows = connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                    actual_columns = {str(row["name"]) for row in column_rows}
                    if not expected_columns.issubset(actual_columns):
                        invalid_schema = True
                        break
                index_rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
                actual_indexes = {str(row["name"]) for row in index_rows}
                if not _REQUIRED_RUNTIME_INDEXES.issubset(actual_indexes):
                    invalid_schema = True
                for table, expected_keys in _REQUIRED_RUNTIME_FOREIGN_KEYS.items():
                    foreign_rows = connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    ).fetchall()
                    actual_keys = {
                        (
                            str(row["from"]),
                            str(row["table"]),
                            str(row["to"]),
                            str(row["on_delete"]),
                        )
                        for row in foreign_rows
                    }
                    if not expected_keys.issubset(actual_keys):
                        invalid_schema = True
                        break
                if invalid_schema:
                    snapshot.issues.append(
                        RuntimeConsistencyIssue(
                            code="runtime_schema_incomplete",
                            severity="error",
                            detail=(
                                "runtime database is missing required columns, indexes, "
                                "or foreign keys"
                            ),
                        )
                    )
                    snapshot.tables = frozenset()
                    return snapshot
                foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
                if foreign_key_rows:
                    snapshot.issues.append(
                        RuntimeConsistencyIssue(
                            code="runtime_foreign_key_violation",
                            severity="error",
                            detail=(
                                f"runtime database contains {len(foreign_key_rows)} "
                                "foreign-key relationship violation(s)"
                            ),
                        )
                    )
                thread_rows = connection.execute(
                    "SELECT * FROM runtime_threads ORDER BY updated_at DESC, id"
                ).fetchall()
                turn_rows = connection.execute(
                    "SELECT * FROM runtime_turns ORDER BY created_at, id"
                ).fetchall()
                item_rows = connection.execute(
                    "SELECT * FROM runtime_turn_items ORDER BY created_at, id"
                ).fetchall()
                event_rows = connection.execute(
                    "SELECT * FROM runtime_events ORDER BY thread_id, seq"
                ).fetchall()
                facade_rows = connection.execute(
                    "SELECT * FROM runtime_session_facades ORDER BY thread_id"
                ).fetchall()
                snapshot.thread_count = len(thread_rows)
                snapshot.turn_count = len(turn_rows)
                for row in thread_rows:
                    if _is_future_runtime_record_schema(row["schema_version"]):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="unsupported_runtime_thread_schema",
                                severity="error",
                                thread_id=_safe_identifier(row["id"]),
                                detail="runtime thread row requires a newer CodeRook version",
                            )
                        )
                        continue
                    try:
                        snapshot.threads.append(_thread_from_row(row))
                    except (
                        IndexError,
                        KeyError,
                        RuntimeStoreError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="invalid_runtime_thread_record",
                                severity="error",
                                thread_id=_safe_identifier(row["id"]),
                                detail="runtime thread row failed strict validation",
                            )
                        )
                for row in turn_rows:
                    if _is_future_runtime_record_schema(row["schema_version"]):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="unsupported_runtime_turn_schema",
                                severity="error",
                                thread_id=_safe_identifier(row["thread_id"]),
                                turn_id=_safe_identifier(row["id"]),
                                detail="runtime turn row requires a newer CodeRook version",
                            )
                        )
                        continue
                    try:
                        snapshot.turns.append(_turn_from_row(row))
                    except (
                        IndexError,
                        KeyError,
                        RuntimeStoreError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="invalid_runtime_turn_record",
                                severity="error",
                                thread_id=_safe_identifier(row["thread_id"]),
                                turn_id=_safe_identifier(row["id"]),
                                detail="runtime turn row failed strict validation",
                            )
                        )
                for row in item_rows:
                    if _is_future_runtime_record_schema(row["schema_version"]):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="unsupported_runtime_item_schema",
                                severity="error",
                                turn_id=_safe_identifier(row["turn_id"]),
                                detail=(
                                    "runtime turn item row requires a newer CodeRook version"
                                ),
                            )
                        )
                        continue
                    try:
                        _item_from_row(row)
                    except (
                        IndexError,
                        KeyError,
                        RuntimeStoreError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="invalid_runtime_item_record",
                                severity="error",
                                turn_id=_safe_identifier(row["turn_id"]),
                                detail="runtime turn item row failed strict validation",
                            )
                        )
                for row in event_rows:
                    if _is_future_runtime_record_schema(row["schema_version"]):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="unsupported_runtime_event_schema",
                                severity="error",
                                thread_id=_safe_identifier(row["thread_id"]),
                                turn_id=_safe_identifier(row["turn_id"]),
                                detail="runtime event row requires a newer CodeRook version",
                            )
                        )
                        continue
                    try:
                        _event_from_row(row)
                    except (
                        IndexError,
                        KeyError,
                        RuntimeStoreError,
                        TypeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="invalid_runtime_event_record",
                                severity="error",
                                thread_id=_safe_identifier(row["thread_id"]),
                                turn_id=_safe_identifier(row["turn_id"]),
                                detail="runtime event row failed strict validation",
                            )
                        )
                for row in facade_rows:
                    if _is_future_runtime_record_schema(row["schema_version"]):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="unsupported_runtime_facade_schema",
                                severity="error",
                                thread_id=_safe_identifier(row["thread_id"]),
                                detail=(
                                    "runtime session facade row requires a newer CodeRook "
                                    "version"
                                ),
                            )
                        )
                        continue
                    try:
                        SessionFacadeRecord(
                            thread_id=row["thread_id"],
                            mode=row["mode"],
                            parent_thread_id=row["parent_thread_id"],
                            schema_version=row["schema_version"],
                        )
                    except (IndexError, KeyError, TypeError, ValueError):
                        snapshot.issues.append(
                            RuntimeConsistencyIssue(
                                code="invalid_runtime_facade_record",
                                severity="error",
                                thread_id=_safe_identifier(row["thread_id"]),
                                detail="runtime session facade row failed strict validation",
                            )
                        )
        except (OSError, sqlite3.DatabaseError):
            snapshot.issues.append(
                RuntimeConsistencyIssue(
                    code="runtime_database_unreadable",
                    severity="error",
                    detail="runtime database could not be read safely",
                )
            )
        return snapshot

    # 检查每个 thread 的事件序号以及缺失或漂移的下一个序号计数器
    def _inspect_event_sequences(self) -> list[RuntimeConsistencyIssue]:
        issues: list[RuntimeConsistencyIssue] = []
        try:
            with _read_only_database(self._runtime.path) as connection:
                thread_rows = connection.execute(
                    "SELECT id FROM runtime_threads ORDER BY id"
                ).fetchall()
                counter_rows = connection.execute(
                    "SELECT thread_id, next_seq FROM runtime_event_counters "
                    "ORDER BY thread_id"
                ).fetchall()
                counters = {
                    str(row["thread_id"]): int(row["next_seq"]) for row in counter_rows
                }
                thread_ids = {str(row["id"]) for row in thread_rows}
                for thread_id in sorted(thread_ids):
                    rows = connection.execute(
                        "SELECT seq FROM runtime_events WHERE thread_id = ? ORDER BY seq",
                        (thread_id,),
                    ).fetchall()
                    sequences = [int(row["seq"]) for row in rows]
                    if any(seq != index for index, seq in enumerate(sequences, 1)):
                        issues.append(
                            RuntimeConsistencyIssue(
                                code="event_sequence_gap",
                                severity="error",
                                thread_id=_safe_identifier(thread_id),
                                detail=(
                                    f"{len(sequences)} event(s) do not form a contiguous "
                                    "sequence starting at one"
                                ),
                            )
                        )
                    expected_next = (sequences[-1] + 1) if sequences else 1
                    actual_next = counters.get(thread_id)
                    if actual_next is None:
                        issues.append(
                            RuntimeConsistencyIssue(
                                code="missing_event_counter",
                                severity="error",
                                thread_id=_safe_identifier(thread_id),
                                detail=f"event counter is missing; expected {expected_next}",
                                repairable=True,
                            )
                        )
                    elif actual_next != expected_next:
                        issues.append(
                            RuntimeConsistencyIssue(
                                code="event_counter_mismatch",
                                severity="error",
                                thread_id=_safe_identifier(thread_id),
                                detail=(
                                    f"next_seq={actual_next}, expected {expected_next}"
                                ),
                                repairable=True,
                            )
                        )
                for thread_id in sorted(set(counters) - thread_ids):
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="orphan_event_counter",
                            severity="error",
                            thread_id=_safe_identifier(thread_id),
                            detail="event counter has no runtime thread",
                        )
                    )
        except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
            issues.append(
                RuntimeConsistencyIssue(
                    code="event_sequence_unreadable",
                    severity="error",
                    detail="runtime event sequences could not be read safely",
                )
            )
        return issues

    # 显式隔离坏文件并幂等修复缺失投影与事件计数器
    async def repair(self) -> RuntimeReconcileReport:
        before = self.inspect()
        if any(
            issue.code in {"unsafe_repair_state_root", "unsafe_repair_journal_path"}
            for issue in before.issues
        ):
            return before
        repaired: list[str] = []
        repair_failures: list[RuntimeConsistencyIssue] = []
        state = self._inspect_state_documents()
        quarantined = 0
        for record in state.corrupt_records:
            moved = quarantine_invalid_file(
                record.path,
                category=record.category,
                reason=f"record failed strict {record.category} validation",
                state_root=self._state_root,
                expected_fingerprint=record.fingerprint,
            )
            quarantined += int(moved is not None)
        if quarantined:
            repaired.append("state_quarantine")

        initialization_codes = {
            "runtime_database_missing",
            "runtime_schema_upgrade_required",
        }
        runtime_writes_allowed = not any(
            issue.code in _RUNTIME_REPAIR_BLOCKERS - {"runtime_schema_upgrade_required"}
            for issue in before.issues
        )
        if runtime_writes_allowed and any(
            issue.code in initialization_codes and issue.repairable
            for issue in before.issues
        ):
            try:
                migrate_database(self._runtime.path)
                repaired.append("runtime_schema_upgrade")
            except (OSError, sqlite3.DatabaseError, ValueError):
                repair_failures.append(
                    RuntimeConsistencyIssue(
                        code="runtime_schema_repair_failed",
                        severity="error",
                        detail="runtime schema repair failed safely",
                    )
                )

        working = self.inspect()
        runtime_writes_allowed = not any(
            issue.code in _RUNTIME_REPAIR_BLOCKERS
            for issue in working.issues
        )
        projection_codes = {"missing_thread_projection", "missing_turn_projection"}
        if runtime_writes_allowed and any(
            issue.code in projection_codes and issue.repairable
            for issue in working.issues
        ):
            try:
                service = RuntimeService(self._runtime, workspace=self._workspace)
                valid_sessions = self._inspect_state_documents().sessions
                ranges: dict[str, tuple[str, str]] = {}
                for session in valid_sessions:
                    try:
                        ranges.update(self._sessions.run_time_ranges(session.id))
                    except (OSError, UnicodeError, ValueError):
                        continue
                await service.bootstrap_sessions(valid_sessions, turn_times=ranges)
                repaired.append("projection_bootstrap")
            except (OSError, sqlite3.DatabaseError, ValueError):
                repair_failures.append(
                    RuntimeConsistencyIssue(
                        code="projection_repair_failed",
                        severity="error",
                        detail="runtime projection repair failed safely",
                    )
                )

        working = self.inspect()
        counter_threads = {
            issue.thread_id
            for issue in working.issues
            if issue.code in {"event_counter_mismatch", "missing_event_counter"}
            and issue.repairable
            and issue.thread_id
        }
        if runtime_writes_allowed and counter_threads:
            try:
                await asyncio.to_thread(self._repair_event_counters, counter_threads)
                repaired.append("event_counters")
            except (OSError, sqlite3.DatabaseError, ValueError):
                repair_failures.append(
                    RuntimeConsistencyIssue(
                        code="event_counter_repair_failed",
                        severity="error",
                        detail="event counter repair failed safely",
                    )
                )
        after = self.inspect()
        if repair_failures:
            combined = [*after.issues, *repair_failures]
            after = after.model_copy(update={"issues": combined, "healthy": False})
        after = after.model_copy(update={"repaired": repaired})
        try:
            self._append_journal(before, after)
        except (OSError, RuntimeError, ValueError):
            journal_failure = RuntimeConsistencyIssue(
                code="repair_journal_write_failed",
                severity="error",
                detail="runtime repair journal could not be written safely",
            )
            after = after.model_copy(
                update={"issues": [*after.issues, journal_failure], "healthy": False}
            )
        return after

    # 将指定 thread 的 counter 原子新增或重置为当前最大事件序号加一
    def _repair_event_counters(self, thread_ids: set[str]) -> None:
        with connect_database(self._runtime.path) as connection:
            for thread_id in sorted(thread_ids):
                row = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM runtime_events "
                    "WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                next_seq = int(row["max_seq"]) + 1
                connection.execute(
                    """
                    INSERT INTO runtime_event_counters (thread_id, next_seq)
                    VALUES (?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET next_seq = excluded.next_seq
                    """,
                    (thread_id, next_seq),
                )

    # 追加不覆盖历史的修复日志，保留修复前后问题与动作
    def _append_journal(
        self,
        before: RuntimeReconcileReport,
        after: RuntimeReconcileReport,
    ) -> None:
        payload = {
            "schema_version": 1,
            "ts": datetime.now(UTC).isoformat(),
            "before": before.model_dump(mode="json"),
            "after": after.model_dump(mode="json"),
            "repaired": after.repaired,
        }
        descriptor = self._open_repair_journal()
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    # 以 no-follow 身份复核和独占创建语义打开边界内 repair journal
    def _open_repair_journal(self) -> int:
        state_root = self._state_root.absolute()
        journal = self._journal_path.absolute()
        if journal.parent != state_root:
            raise ValueError("repair journal is outside the state root")
        if not os.path.lexists(state_root):
            state_root.mkdir(parents=True, exist_ok=False)
        root_before = os.lstat(state_root)
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            raise ValueError("repair state root is unsafe")
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        for _attempt in range(2):
            if os.path.lexists(journal):
                before = os.lstat(journal)
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise ValueError("repair journal is unsafe")
                descriptor = os.open(journal, flags)
            else:
                try:
                    descriptor = os.open(
                        journal,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                except FileExistsError:
                    continue
                before = os.fstat(descriptor)
            try:
                opened = os.fstat(descriptor)
                root_after = os.lstat(state_root)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
                    != (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
                    or (root_after.st_dev, root_after.st_ino, stat.S_IFMT(root_after.st_mode))
                    != (
                        root_before.st_dev,
                        root_before.st_ino,
                        stat.S_IFMT(root_before.st_mode),
                    )
                ):
                    raise ValueError("repair journal identity changed while opening")
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor
        raise OSError("repair journal path changed while opening")

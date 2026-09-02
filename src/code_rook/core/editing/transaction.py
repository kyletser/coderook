from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.editing.engine import (
    _fsync_directory,
    _write_temp_file,
    content_hash,
)


class FileTransactionError(RuntimeError):
    # 初始化带稳定错误码的文件事务异常
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FileMutation:
    path: Path
    original: bytes | None
    updated: bytes | None


@dataclass
class _CommitState:
    mutation: FileMutation
    staged: Path | None
    backup: Path | None = None
    backed_up: bool = False
    installed: bool = False


# 恢复指定工作区上次崩溃遗留的文件事务，未提交事务一律回滚到原状态
def recover_file_transactions(workspace_root: Path) -> int:
    resolved_root = workspace_root.resolve()
    journal_dir = _journal_directory(resolved_root)
    if not journal_dir.exists():
        return 0
    recovered = 0
    for journal in sorted(journal_dir.glob("*.json")):
        try:
            raw = json.loads(journal.read_text(encoding="utf-8"))
            if raw.get("version") != 1 or not isinstance(raw.get("entries"), list):
                raise ValueError("unsupported transaction journal")
            committed = raw.get("state") == "committed"
            entries = list(raw["entries"])
            for entry in reversed(entries):
                if not isinstance(entry, dict):
                    raise ValueError("invalid transaction journal entry")
                target = _journal_path(resolved_root, entry.get("path"))
                staged = _journal_path(resolved_root, entry.get("staged"))
                backup = _journal_path(resolved_root, entry.get("backup"))
                original_exists = bool(entry.get("original_exists"))
                if not committed:
                    if original_exists and backup is not None and backup.exists():
                        if target is not None:
                            target.unlink(missing_ok=True)
                            os.replace(backup, target)
                    elif not original_exists and staged is not None and not staged.exists():
                        if target is not None:
                            target.unlink(missing_ok=True)
                if staged is not None:
                    staged.unlink(missing_ok=True)
                if backup is not None:
                    backup.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            recovered += 1
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise FileTransactionError(
                "recovery_failed",
                f"could not recover file transaction {journal}: {exc}",
            ) from exc
    return recovered


# 以可恢复日志和逐文件备份原子提交一组工作区文件修改
def apply_file_transaction(workspace_root: Path, mutations: list[FileMutation]) -> None:
    if not mutations:
        raise FileTransactionError("empty_transaction", "transaction has no file changes")
    resolved_root = workspace_root.resolve()
    recover_file_transactions(resolved_root)
    paths = [mutation.path for mutation in mutations]
    if len(set(paths)) != len(paths):
        raise FileTransactionError("duplicate_path", "transaction contains duplicate paths")
    for path in paths:
        try:
            path.resolve(strict=False).relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise FileTransactionError(
                "outside_workspace",
                f"transaction path is outside workspace: {path}",
            ) from exc

    _assert_current(mutations)
    states: list[_CommitState] = []
    created_dirs: set[Path] = set()
    preserve_backups = False
    journal: Path | None = None
    try:
        for mutation in mutations:
            staged = None
            if mutation.updated is not None:
                _create_parents(mutation.path.parent, resolved_root, created_dirs)
                mode = mutation.path.stat().st_mode & 0o7777 if mutation.original else None
                staged = _write_temp_file(mutation.path, mutation.updated, mode)
            states.append(_CommitState(mutation=mutation, staged=staged))

        _assert_current(mutations)
        for state in states:
            if state.mutation.original is not None:
                state.backup = _reserve_backup(state.mutation.path)
        try:
            journal = _write_transaction_journal(resolved_root, states, state="prepared")
        except OSError as exc:
            raise FileTransactionError(
                "commit_failed",
                f"transaction journal could not be persisted: {exc}",
            ) from exc
        try:
            for state in states:
                mutation = state.mutation
                if mutation.original is not None:
                    assert state.backup is not None
                    os.replace(mutation.path, state.backup)
                    state.backed_up = True
                if state.staged is not None:
                    os.replace(state.staged, mutation.path)
                    state.staged = None
                    state.installed = True
        except BaseException as exc:
            rollback_errors = _rollback(states)
            if rollback_errors:
                preserve_backups = True
                detail = "; ".join(rollback_errors)
                raise FileTransactionError(
                    "rollback_failed",
                    f"patch commit failed ({exc}); rollback was incomplete: {detail}",
                ) from exc
            raise FileTransactionError("commit_failed", f"patch commit failed: {exc}") from exc

        for parent in {mutation.path.parent for mutation in mutations}:
            _fsync_directory(parent)
        assert journal is not None
        _write_transaction_journal(
            resolved_root,
            states,
            state="committed",
            path=journal,
        )
        for state in states:
            if state.backup is not None and not preserve_backups:
                state.backup.unlink(missing_ok=True)
                state.backup = None
        journal.unlink(missing_ok=True)
        journal = None
    finally:
        for state in states:
            if state.staged is not None:
                state.staged.unlink(missing_ok=True)
            if state.backup is not None and not preserve_backups:
                state.backup.unlink(missing_ok=True)
        if journal is not None and not preserve_backups:
            journal.unlink(missing_ok=True)
        _remove_empty_dirs(created_dirs)


# 返回工作区专属的用户级事务日志目录，避免仓库内容伪造恢复指令
def _journal_directory(root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:24]
    return Path.home() / ".coderook" / "transactions" / digest


# 将日志中的相对路径解析到工作区内，空路径表示该项不存在
def _journal_path(root: Path, raw: object) -> Path | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError("transaction journal path must be a string")
    candidate = (root / raw).resolve(strict=False)
    candidate.relative_to(root)
    return candidate


# 原子写入当前文件事务阶段，并同步目录保证强杀后日志可见
def _write_transaction_journal(
    root: Path,
    states: list[_CommitState],
    *,
    state: str,
    path: Path | None = None,
) -> Path:
    journal_dir = _journal_directory(root)
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal = path or journal_dir / f"{uuid.uuid4().hex}.json"
    payload = {
        "version": 1,
        "state": state,
        "entries": [
            {
                "path": item.mutation.path.resolve(strict=False).relative_to(root).as_posix(),
                "staged": (
                    item.staged.resolve(strict=False).relative_to(root).as_posix()
                    if item.staged is not None
                    else ""
                ),
                "backup": (
                    item.backup.resolve(strict=False).relative_to(root).as_posix()
                    if item.backup is not None
                    else ""
                ),
                "original_exists": item.mutation.original is not None,
            }
            for item in states
        ],
    }
    descriptor, raw_temp = tempfile.mkstemp(dir=journal_dir, suffix=".tmp")
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal)
        _fsync_directory(journal_dir)
    finally:
        temporary.unlink(missing_ok=True)
    return journal


# 校验提交前文件内容仍与准备阶段一致以阻止并发覆盖
def _assert_current(mutations: list[FileMutation]) -> None:
    for mutation in mutations:
        if mutation.original is None:
            if mutation.path.exists():
                raise FileTransactionError(
                    "concurrent_change",
                    f"file was created while patch was prepared: {mutation.path}",
                )
            continue
        try:
            current = mutation.path.read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise FileTransactionError(
                "concurrent_change",
                f"file disappeared or changed type while patch was prepared: {mutation.path}",
            ) from exc
        if content_hash(current) != content_hash(mutation.original):
            raise FileTransactionError(
                "concurrent_change",
                f"file changed while patch was prepared: {mutation.path}",
            )


# 在目标目录预留唯一备份路径但不留下占位文件
def _reserve_backup(path: Path) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".bak",
    )
    os.close(descriptor)
    backup = Path(raw_path)
    backup.unlink()
    return backup


# 逆序撤销已安装内容并仅恢复确实完成的备份
def _rollback(states: list[_CommitState]) -> list[str]:
    errors: list[str] = []
    for state in reversed(states):
        try:
            if state.installed:
                state.mutation.path.unlink(missing_ok=True)
                state.installed = False
            if state.backed_up and state.backup is not None:
                os.replace(state.backup, state.mutation.path)
                state.backup = None
                state.backed_up = False
        except OSError as exc:
            backup_note = (
                f"; backup preserved at {state.backup}" if state.backup is not None else ""
            )
            errors.append(f"{state.mutation.path}: {exc}{backup_note}")
    return errors


# 创建目标文件缺失的父目录并记录本事务新增目录
def _create_parents(parent: Path, root: Path, created_dirs: set[Path]) -> None:
    missing: list[Path] = []
    current = parent
    while current != root and not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        created_dirs.add(directory)


# 逆序清理事务失败后仍为空的新增父目录
def _remove_empty_dirs(created_dirs: set[Path]) -> None:
    for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

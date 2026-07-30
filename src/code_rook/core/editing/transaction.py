from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.editing.engine import (
    _fsync_directory,
    _write_temp_file,
    content_hash,
)


class FileTransactionError(RuntimeError):
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
    installed: bool = False


def apply_file_transaction(workspace_root: Path, mutations: list[FileMutation]) -> None:
    if not mutations:
        raise FileTransactionError("empty_transaction", "transaction has no file changes")
    resolved_root = workspace_root.resolve()
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
    try:
        for mutation in mutations:
            staged = None
            if mutation.updated is not None:
                _create_parents(mutation.path.parent, resolved_root, created_dirs)
                mode = mutation.path.stat().st_mode & 0o7777 if mutation.original else None
                staged = _write_temp_file(mutation.path, mutation.updated, mode)
            states.append(_CommitState(mutation=mutation, staged=staged))

        _assert_current(mutations)
        try:
            for state in states:
                mutation = state.mutation
                if mutation.original is not None:
                    backup = _reserve_backup(mutation.path)
                    os.replace(mutation.path, backup)
                    state.backup = backup
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

        for state in states:
            if state.backup is not None and not preserve_backups:
                state.backup.unlink(missing_ok=True)
                state.backup = None
        for parent in {mutation.path.parent for mutation in mutations}:
            _fsync_directory(parent)
    finally:
        for state in states:
            if state.staged is not None:
                state.staged.unlink(missing_ok=True)
            if state.backup is not None:
                state.backup.unlink(missing_ok=True)
        _remove_empty_dirs(created_dirs)


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


def _rollback(states: list[_CommitState]) -> list[str]:
    errors: list[str] = []
    for state in reversed(states):
        try:
            if state.installed:
                state.mutation.path.unlink(missing_ok=True)
                state.installed = False
            if state.backup is not None:
                os.replace(state.backup, state.mutation.path)
                state.backup = None
        except OSError as exc:
            backup_note = (
                f"; backup preserved at {state.backup}" if state.backup is not None else ""
            )
            errors.append(f"{state.mutation.path}: {exc}{backup_note}")
    return errors


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


def _remove_empty_dirs(created_dirs: set[Path]) -> None:
    for directory in sorted(created_dirs, key=lambda path: len(path.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass

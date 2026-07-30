from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_rook.core.editing import (
    FileMutation,
    FileTransactionError,
    apply_file_transaction,
    atomic_write_bytes,
    content_hash,
)
from code_rook.core.workspace import WorkspaceBoundary

_CHECKPOINT_ID_RE = re.compile(r"\d{8}T\d{6}-[0-9a-f]{8}\Z")
_BLOB_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MANIFEST_VERSION = 1


class CheckpointError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        conflicts: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.conflicts = conflicts or []


@dataclass(frozen=True)
class CheckpointInfo:
    checkpoint_id: str
    label: str
    created_at: str
    status: str
    paths: list[str]


@dataclass(frozen=True)
class RewindOutcome:
    checkpoint_id: str
    restored: list[str]
    already_restored: list[str]


@dataclass(frozen=True)
class _FileState:
    exists: bool
    digest: str | None
    content: bytes | None


class CheckpointStore:
    def __init__(self, root: Path, boundary: WorkspaceBoundary) -> None:
        self._root = root.resolve()
        self._boundary = boundary
        self._manifests = self._root / "manifests"
        self._blobs = self._root / "blobs"
        self._manifests.mkdir(parents=True, exist_ok=True)
        self._blobs.mkdir(parents=True, exist_ok=True)

    def create(self, mutations: list[FileMutation], *, label: str) -> str:
        if not mutations:
            raise CheckpointError("empty_checkpoint", "checkpoint has no file mutations")
        if len({mutation.path for mutation in mutations}) != len(mutations):
            raise CheckpointError("duplicate_path", "checkpoint contains duplicate file paths")

        checkpoint_id = _new_checkpoint_id()
        entries: list[dict[str, object]] = []
        for mutation in mutations:
            path = self._validated_path(mutation.path)
            relative = path.relative_to(self._boundary.root).as_posix()
            current = _read_state(path)
            expected_before = _state_from_content(mutation.original)
            if not _same_state(current, expected_before):
                raise CheckpointError(
                    "concurrent_change",
                    f"file changed before checkpoint capture: {relative}",
                    conflicts=[relative],
                )
            before_blob = None
            if mutation.original is not None:
                before_blob = content_hash(mutation.original).removeprefix("sha256:")
                self._write_blob(before_blob, mutation.original)
            after = _state_from_content(mutation.updated)
            entries.append({
                "path": relative,
                "before_exists": expected_before.exists,
                "before_hash": expected_before.digest,
                "before_blob": before_blob,
                "after_exists": after.exists,
                "after_hash": after.digest,
            })

        created_at = datetime.now(UTC).isoformat()
        manifest: dict[str, object] = {
            "version": _MANIFEST_VERSION,
            "checkpoint_id": checkpoint_id,
            "label": label[:100],
            "created_at": created_at,
            "status": "ready",
            "files": entries,
        }
        self._write_manifest(checkpoint_id, manifest)
        return checkpoint_id

    def discard(self, checkpoint_id: str) -> None:
        try:
            self._manifest_path(checkpoint_id).unlink(missing_ok=True)
        except OSError:
            pass

    def list_checkpoints(self) -> list[CheckpointInfo]:
        checkpoints: list[CheckpointInfo] = []
        for path in self._manifests.glob("*.json"):
            checkpoint_id = path.stem
            if _CHECKPOINT_ID_RE.fullmatch(checkpoint_id) is None:
                continue
            try:
                manifest = self._load_manifest(checkpoint_id)
                entries = _manifest_files(manifest)
            except CheckpointError:
                continue
            checkpoints.append(
                CheckpointInfo(
                    checkpoint_id=checkpoint_id,
                    label=str(manifest.get("label", "")),
                    created_at=str(manifest.get("created_at", "")),
                    status=str(manifest.get("status", "unknown")),
                    paths=[str(entry["path"]) for entry in entries],
                )
            )
        checkpoints.sort(key=lambda item: item.created_at, reverse=True)
        return checkpoints

    def rewind(self, checkpoint_id: str | None = None) -> RewindOutcome:
        selected_id = checkpoint_id or self._latest_ready_id()
        manifest = self._load_manifest(selected_id)
        entries = _manifest_files(manifest)
        conflicts: list[str] = []
        mutations: list[FileMutation] = []
        restored: list[str] = []
        already_restored: list[str] = []

        for entry in entries:
            relative = str(entry["path"])
            try:
                path = self._boundary.resolve(relative)
            except PermissionError as exc:
                raise CheckpointError(
                    "manifest_invalid",
                    f"checkpoint path is outside the workspace: {relative}",
                ) from exc
            current = _read_state(path)
            before = _entry_state(entry, "before")
            after = _entry_state(entry, "after")
            if _same_state(current, before):
                already_restored.append(relative)
                continue
            if not _same_state(current, after):
                conflicts.append(relative)
                continue
            before_content = self._read_before_blob(entry) if before.exists else None
            mutations.append(
                FileMutation(path=path, original=current.content, updated=before_content)
            )
            restored.append(relative)

        if conflicts:
            raise CheckpointError(
                "rewind_conflict",
                "files changed after the checkpoint and cannot be safely rewound: "
                + ", ".join(conflicts),
                conflicts=conflicts,
            )
        if mutations:
            try:
                apply_file_transaction(self._boundary.root, mutations)
            except FileTransactionError as exc:
                raise CheckpointError(exc.code, str(exc)) from exc

        manifest["status"] = "rewound"
        manifest["rewound_at"] = datetime.now(UTC).isoformat()
        self._write_manifest(selected_id, manifest)
        return RewindOutcome(
            checkpoint_id=selected_id,
            restored=restored,
            already_restored=already_restored,
        )

    def _latest_ready_id(self) -> str:
        for checkpoint in self.list_checkpoints():
            if checkpoint.status == "ready":
                return checkpoint.checkpoint_id
        raise CheckpointError("checkpoint_not_found", "no ready checkpoint is available")

    def _validated_path(self, path: Path) -> Path:
        resolved = self._boundary.resolve(str(path))
        if resolved != path.resolve(strict=False):
            raise CheckpointError("outside_workspace", f"path is outside workspace: {path}")
        return resolved

    def _write_blob(self, digest: str, content: bytes) -> None:
        path = self._blobs / digest
        if path.exists():
            try:
                existing = path.read_bytes()
            except OSError as exc:
                raise CheckpointError(
                    "storage_error",
                    f"cannot read checkpoint blob: {digest}",
                ) from exc
            if content_hash(existing).removeprefix("sha256:") != digest:
                raise CheckpointError("blob_corrupt", f"checkpoint blob is corrupt: {digest}")
            return
        try:
            atomic_write_bytes(path, content)
        except OSError as exc:
            raise CheckpointError(
                "storage_error",
                f"cannot write checkpoint blob: {digest}",
            ) from exc

    def _read_before_blob(self, entry: dict[str, Any]) -> bytes:
        blob_name = entry.get("before_blob")
        expected_hash = entry.get("before_hash")
        if (
            not isinstance(blob_name, str)
            or _BLOB_RE.fullmatch(blob_name) is None
            or not isinstance(expected_hash, str)
            or expected_hash != f"sha256:{blob_name}"
        ):
            raise CheckpointError("manifest_invalid", "checkpoint before blob metadata is invalid")
        path = self._blobs / blob_name
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise CheckpointError(
                "blob_missing",
                f"checkpoint blob is missing: {blob_name}",
            ) from exc
        except OSError as exc:
            raise CheckpointError(
                "storage_error",
                f"cannot read checkpoint blob: {blob_name}",
            ) from exc
        if content_hash(content) != expected_hash:
            raise CheckpointError("blob_corrupt", f"checkpoint blob is corrupt: {blob_name}")
        return content

    def _manifest_path(self, checkpoint_id: str) -> Path:
        if _CHECKPOINT_ID_RE.fullmatch(checkpoint_id) is None:
            raise CheckpointError("invalid_checkpoint_id", "invalid checkpoint id")
        return self._manifests / f"{checkpoint_id}.json"

    def _write_manifest(self, checkpoint_id: str, manifest: dict[str, object]) -> None:
        encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            atomic_write_bytes(self._manifest_path(checkpoint_id), encoded)
        except OSError as exc:
            raise CheckpointError(
                "storage_error",
                f"cannot write checkpoint manifest: {checkpoint_id}",
            ) from exc

    def _load_manifest(self, checkpoint_id: str) -> dict[str, Any]:
        path = self._manifest_path(checkpoint_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CheckpointError(
                "checkpoint_not_found",
                f"checkpoint does not exist: {checkpoint_id}",
            ) from exc
        except (json.JSONDecodeError, OSError) as exc:
            raise CheckpointError("manifest_invalid", "checkpoint manifest is unreadable") from exc
        if not isinstance(raw, dict) or raw.get("version") != _MANIFEST_VERSION:
            raise CheckpointError("manifest_invalid", "checkpoint manifest version is invalid")
        if raw.get("checkpoint_id") != checkpoint_id:
            raise CheckpointError("manifest_invalid", "checkpoint manifest id does not match")
        return raw


def _new_checkpoint_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _read_state(path: Path) -> _FileState:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return _FileState(exists=False, digest=None, content=None)
    except IsADirectoryError as exc:
        raise CheckpointError("path_not_file", f"checkpoint path is not a file: {path}") from exc
    return _FileState(exists=True, digest=content_hash(content), content=content)


def _state_from_content(content: bytes | None) -> _FileState:
    return _FileState(
        exists=content is not None,
        digest=content_hash(content) if content is not None else None,
        content=content,
    )


def _same_state(left: _FileState, right: _FileState) -> bool:
    return left.exists == right.exists and left.digest == right.digest


def _manifest_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise CheckpointError("manifest_invalid", "checkpoint file list is invalid")
    entries: list[dict[str, Any]] = []
    for raw_entry in raw_files:
        if not isinstance(raw_entry, dict) or not isinstance(raw_entry.get("path"), str):
            raise CheckpointError("manifest_invalid", "checkpoint file entry is invalid")
        entries.append(raw_entry)
    return entries


def _entry_state(entry: dict[str, Any], prefix: str) -> _FileState:
    exists = entry.get(f"{prefix}_exists")
    digest = entry.get(f"{prefix}_hash")
    if not isinstance(exists, bool):
        raise CheckpointError("manifest_invalid", f"checkpoint {prefix} state is invalid")
    if exists:
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise CheckpointError("manifest_invalid", f"checkpoint {prefix} hash is invalid")
    if not exists and digest is not None:
        raise CheckpointError("manifest_invalid", f"checkpoint {prefix} hash must be null")
    return _FileState(exists=exists, digest=digest, content=None)

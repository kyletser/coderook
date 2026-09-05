from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from code_rook.core.workspace import WorkspaceBoundary

if TYPE_CHECKING:
    from code_rook.core.checkpoints import CheckpointStore

MAX_EDIT_BYTES = 1 * 1024 * 1024
MAX_DIFF_CHARS = 12_000
_HASH_RE = re.compile(r"(?:sha256:)?([0-9a-fA-F]{64})\Z")


class EditError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EditOutcome:
    path: str
    replacements: int
    old_hash: str
    new_hash: str
    bytes_written: int
    diff: str
    diff_truncated: bool
    additions: int
    deletions: int
    checkpoint_id: str | None


def content_hash(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def normalize_content_hash(value: str) -> str:
    match = _HASH_RE.fullmatch(value.strip())
    if match is None:
        raise EditError(
            "invalid_hash",
            "expected_hash must be a SHA-256 digest, with an optional 'sha256:' prefix",
        )
    return f"sha256:{match.group(1).lower()}"


class EditEngine:
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        max_bytes: int = MAX_EDIT_BYTES,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self._boundary = boundary
        self._max_bytes = max_bytes
        self._checkpoint_store = checkpoint_store

    def edit(
        self,
        path_value: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
        expected_hash: str | None = None,
    ) -> EditOutcome:
        if not old_text:
            raise EditError("invalid_edit", "old_text must be non-empty")
        if old_text == new_text:
            raise EditError("invalid_edit", "old_text and new_text must differ")

        path = self._boundary.resolve(path_value)
        if not path.is_file():
            raise EditError("not_found", f"file does not exist: {path_value}")

        raw = path.read_bytes()
        if len(raw) > self._max_bytes:
            raise EditError(
                "file_too_large",
                f"file is {len(raw)} bytes; edit limit is {self._max_bytes} bytes",
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditError("not_utf8", f"file is not valid UTF-8: {path_value}") from exc

        old_hash = content_hash(raw)
        if expected_hash is not None:
            normalized_hash = normalize_content_hash(expected_hash)
            if normalized_hash != old_hash:
                raise EditError(
                    "hash_mismatch",
                    f"file changed after it was read: expected {normalized_hash}, found {old_hash}",
                )

        match_text = old_text
        replacement_text = new_text
        occurrences = text.count(match_text)
        if occurrences == 0:
            match_text, replacement_text = _adapt_edit_newlines(
                text,
                old_text,
                new_text,
            )
            occurrences = text.count(match_text)
        if occurrences == 0:
            raise EditError("match_not_found", "old_text was not found in the current file")
        if occurrences > 1 and not replace_all:
            raise EditError(
                "ambiguous_match",
                f"old_text matched {occurrences} locations; "
                "provide more context or set replace_all",
            )

        replacements = occurrences if replace_all else 1
        updated = text.replace(
            match_text,
            replacement_text,
            -1 if replace_all else 1,
        )
        encoded = updated.encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise EditError(
                "content_too_large",
                f"edited content is {len(encoded)} bytes; limit is {self._max_bytes} bytes",
            )

        diff, diff_truncated, additions, deletions = _unified_diff(
            path_value,
            text,
            updated,
        )
        checkpoint_id = None
        if self._checkpoint_store is not None:
            from code_rook.core.checkpoints import CheckpointError
            from code_rook.core.editing.transaction import FileMutation

            try:
                checkpoint_id = self._checkpoint_store.create(
                    [FileMutation(path=path, original=raw, updated=encoded)],
                    label="edit_file",
                )
            except CheckpointError as exc:
                raise EditError(exc.code, str(exc)) from exc
        try:
            atomic_write_bytes(path, encoded, expected_hash=old_hash)
        except BaseException:
            if checkpoint_id is not None and self._checkpoint_store is not None:
                self._checkpoint_store.discard(checkpoint_id)
            raise
        return EditOutcome(
            path=path.relative_to(self._boundary.root).as_posix(),
            replacements=replacements,
            old_hash=old_hash,
            new_hash=content_hash(encoded),
            bytes_written=len(encoded),
            diff=diff,
            diff_truncated=diff_truncated,
            additions=additions,
            deletions=deletions,
            checkpoint_id=checkpoint_id,
        )


# 将模型统一换行的多行匹配适配到文件现有行尾，同时保持未编辑区域字节语义不变
def _adapt_edit_newlines(text: str, old_text: str, new_text: str) -> tuple[str, str]:
    if "\n" not in old_text and "\r" not in old_text:
        return old_text, new_text
    if "\r\n" in text:
        newline = "\r\n"
    elif "\r" in text:
        newline = "\r"
    elif "\n" in text:
        newline = "\n"
    else:
        return old_text, new_text

    # 将一种文本的混合行尾统一映射成目标文件的行尾
    def adapt(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)

    return adapt(old_text), adapt(new_text)


def atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    expected_hash: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    temp_path = _write_temp_file(path, content, mode)
    try:
        if expected_hash is not None:
            try:
                current_hash = content_hash(path.read_bytes())
            except FileNotFoundError as exc:
                raise EditError(
                    "concurrent_change",
                    "file was removed while the edit was being prepared",
                ) from exc
            if current_hash != expected_hash:
                raise EditError(
                    "concurrent_change",
                    f"file changed during edit: expected {expected_hash}, found {current_hash}",
                )
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_temp_file(path: Path, content: bytes, mode: int | None) -> Path:
    fd, raw_temp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_path, mode)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# 生成有界 unified diff，并在截断前统计真实增删行
def _unified_diff(path: str, before: str, after: str) -> tuple[str, bool, int, int]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    additions = sum(line.startswith("+") and not line.startswith("+++") for line in lines)
    deletions = sum(line.startswith("-") and not line.startswith("---") for line in lines)
    diff = "".join(lines)
    if len(diff) <= MAX_DIFF_CHARS:
        return diff, False, additions, deletions
    return diff[:MAX_DIFF_CHARS] + "\n[diff truncated]\n", True, additions, deletions

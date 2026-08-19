from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from unidiff import PatchSet  # type: ignore[import-untyped]
from unidiff.errors import UnidiffParseError  # type: ignore[import-untyped]

from code_rook.core.editing import (
    FileMutation,
    FileTransactionError,
    apply_file_transaction,
    content_hash,
)
from code_rook.core.editing.engine import MAX_EDIT_BYTES
from code_rook.core.workspace import WorkspaceBoundary

if TYPE_CHECKING:
    from code_rook.core.checkpoints import CheckpointStore

MAX_PATCH_BYTES = 1 * 1024 * 1024
MAX_PATCH_FILES = 100
MAX_PATCH_HUNKS = 1000
Action = Literal["add", "modify", "delete"]


class PatchError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        hunk: int | None = None,
        line: int | None = None,
        expected: str | None = None,
        actual: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.hunk = hunk
        self.line = line
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class PatchedFileOutcome:
    path: str
    action: Action
    hunks: int
    additions: int
    removals: int
    old_hash: str | None
    new_hash: str | None


@dataclass(frozen=True)
class PatchOutcome:
    files: list[PatchedFileOutcome]
    dry_run: bool
    checkpoint_id: str | None


@dataclass(frozen=True)
class PatchHunkPlan:
    id: str
    path: str
    index: int
    source_start: int
    source_length: int
    target_start: int
    target_length: int
    additions: int
    removals: int
    header: str
    selectable: bool


@dataclass(frozen=True)
class PatchFilePlan:
    path: str
    action: Action
    old_hash: str | None
    new_hash: str | None
    hunks: tuple[PatchHunkPlan, ...]


@dataclass(frozen=True)
class PatchPlan:
    id: str
    patch_text: str
    files: tuple[PatchFilePlan, ...]

    # 返回适合 IPC 审批事件的无原始补丁结构化摘要
    def approval_context(self) -> dict[str, object]:
        return {
            "id": self.id,
            "files": [
                {
                    "path": item.path,
                    "action": item.action,
                    "old_hash": item.old_hash,
                    "new_hash": item.new_hash,
                    "hunks": [
                        {
                            "id": hunk.id,
                            "index": hunk.index,
                            "source_start": hunk.source_start,
                            "source_length": hunk.source_length,
                            "target_start": hunk.target_start,
                            "target_length": hunk.target_length,
                            "additions": hunk.additions,
                            "removals": hunk.removals,
                            "header": hunk.header,
                            "selectable": hunk.selectable,
                        }
                        for hunk in item.hunks
                    ],
                }
                for item in self.files
            ],
        }


@dataclass(frozen=True)
class _TextFormat:
    bom: bool
    newline: str


class PatchEngine:
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self._boundary = boundary
        self._checkpoint_store = checkpoint_store

    # 校验补丁并生成绑定当前文件哈希的稳定逐 hunk 审阅计划
    def plan(self, patch_text: str) -> PatchPlan:
        outcome = self.apply(patch_text, dry_run=True)
        patch_set = _parse_patch_set(patch_text)
        outcomes = {item.path: item for item in outcome.files}
        files: list[PatchFilePlan] = []
        for patched_file in patch_set:
            action, path = _patch_file_identity(patched_file)
            item = outcomes[path]
            all_or_nothing = action in {"add", "delete"} and len(patched_file) > 1
            hunks: list[PatchHunkPlan] = []
            for index, hunk in enumerate(patched_file, start=1):
                hunk_text = str(hunk)
                digest = hashlib.sha256(
                    f"{path}\0{index}\0{hunk_text}".encode()
                ).hexdigest()[:16]
                hunks.append(
                    PatchHunkPlan(
                        id=f"h-{digest}",
                        path=path,
                        index=index,
                        source_start=int(hunk.source_start),
                        source_length=int(hunk.source_length),
                        target_start=int(hunk.target_start),
                        target_length=int(hunk.target_length),
                        additions=sum(1 for line in hunk if line.line_type == "+"),
                        removals=sum(1 for line in hunk if line.line_type == "-"),
                        header=str(hunk.section_header or ""),
                        selectable=not all_or_nothing,
                    )
                )
            files.append(
                PatchFilePlan(
                    path=path,
                    action=action,
                    old_hash=item.old_hash,
                    new_hash=item.new_hash,
                    hunks=tuple(hunks),
                )
            )
        identity = "\n".join(
            f"{item.path}:{item.old_hash or '-'}" for item in files
        )
        plan_id = hashlib.sha256(
            f"{identity}\0{patch_text}".encode()
        ).hexdigest()
        return PatchPlan(id=f"patch-{plan_id[:24]}", patch_text=patch_text, files=tuple(files))

    # 应用审阅计划中选定的 hunk，并拒绝计划生成后的文件漂移
    def apply_plan(
        self,
        plan: PatchPlan,
        selected_hunks: set[str],
    ) -> PatchOutcome:
        self._assert_plan_current(plan)
        current = self.plan(plan.patch_text)
        if current.id != plan.id:
            raise PatchError(
                "stale_patch_plan",
                "workspace changed after patch review; generate a new patch plan",
            )
        known = {hunk.id for item in plan.files for hunk in item.hunks}
        unknown = selected_hunks - known
        if unknown:
            raise PatchError(
                "unknown_hunk",
                f"selected hunk ids are not in patch plan: {', '.join(sorted(unknown))}",
            )
        if not selected_hunks:
            raise PatchError("empty_selection", "at least one patch hunk must be selected")
        selected_patch = _select_patch_hunks(plan, selected_hunks)
        return self.apply(selected_patch)

    # 对比计划记录的原始哈希，先于 hunk 匹配报告明确的审阅后漂移
    def _assert_plan_current(self, plan: PatchPlan) -> None:
        for item in plan.files:
            path = self._boundary.resolve(item.path)
            raw = path.read_bytes() if path.is_file() else None
            current_hash = content_hash(raw) if raw is not None else None
            if current_hash != item.old_hash:
                raise PatchError(
                    "stale_patch_plan",
                    "workspace changed after patch review; generate a new patch plan",
                    path=item.path,
                )

    def apply(self, patch_text: str, *, dry_run: bool = False) -> PatchOutcome:
        encoded_patch = patch_text.encode("utf-8")
        if not patch_text.strip():
            raise PatchError("empty_patch", "patch must be non-empty")
        if len(encoded_patch) > MAX_PATCH_BYTES:
            raise PatchError(
                "patch_too_large",
                f"patch is {len(encoded_patch)} bytes; limit is {MAX_PATCH_BYTES} bytes",
            )
        patch_set = _parse_patch_set(patch_text)
        if not patch_set:
            raise PatchError("empty_patch", "patch contains no file changes")
        if len(patch_set) > MAX_PATCH_FILES:
            raise PatchError(
                "too_many_files",
                f"patch contains {len(patch_set)} files; limit is {MAX_PATCH_FILES}",
            )

        total_hunks = sum(len(patched_file) for patched_file in patch_set)
        if total_hunks > MAX_PATCH_HUNKS:
            raise PatchError(
                "too_many_hunks",
                f"patch contains {total_hunks} hunks; limit is {MAX_PATCH_HUNKS}",
            )

        mutations: list[FileMutation] = []
        outcomes: list[PatchedFileOutcome] = []
        seen_paths: set[Path] = set()
        for patched_file in patch_set:
            mutation, outcome = self._prepare_file(patched_file)
            if mutation.path in seen_paths:
                raise PatchError(
                    "duplicate_path",
                    f"patch contains the same target more than once: {outcome.path}",
                    path=outcome.path,
                )
            seen_paths.add(mutation.path)
            mutations.append(mutation)
            outcomes.append(outcome)

        checkpoint_id = None
        if not dry_run:
            if self._checkpoint_store is not None:
                from code_rook.core.checkpoints import CheckpointError

                try:
                    checkpoint_id = self._checkpoint_store.create(
                        mutations,
                        label="apply_patch",
                    )
                except CheckpointError as exc:
                    raise PatchError(exc.code, str(exc)) from exc
            try:
                apply_file_transaction(self._boundary.root, mutations)
            except FileTransactionError as exc:
                if checkpoint_id is not None and self._checkpoint_store is not None:
                    self._checkpoint_store.discard(checkpoint_id)
                raise PatchError(exc.code, str(exc)) from exc
            except BaseException:
                if checkpoint_id is not None and self._checkpoint_store is not None:
                    self._checkpoint_store.discard(checkpoint_id)
                raise
        return PatchOutcome(
            files=outcomes,
            dry_run=dry_run,
            checkpoint_id=checkpoint_id,
        )

    def _prepare_file(self, patched_file: Any) -> tuple[FileMutation, PatchedFileOutcome]:
        if patched_file.is_binary_file:
            raise PatchError("binary_patch", "binary patches are not supported")
        if patched_file.is_rename:
            raise PatchError(
                "rename_not_supported",
                "rename patches are not supported; add the new file and delete the old file",
            )
        if not patched_file:
            raise PatchError("empty_file_patch", "file patch contains no hunks")

        action: Action
        raw_path: str
        if patched_file.source_file == "/dev/null":
            action = "add"
            raw_path = patched_file.target_file
        elif patched_file.target_file == "/dev/null":
            action = "delete"
            raw_path = patched_file.source_file
        else:
            action = "modify"
            source_path = _clean_patch_path(patched_file.source_file)
            target_path = _clean_patch_path(patched_file.target_file)
            if source_path != target_path:
                raise PatchError(
                    "path_mismatch",
                    f"source and target paths differ: {source_path!r} != {target_path!r}",
                )
            raw_path = source_path

        relative_path = _clean_patch_path(raw_path)
        try:
            path = self._boundary.resolve(relative_path)
        except PermissionError as exc:
            raise PatchError(
                "outside_workspace",
                str(exc),
                path=relative_path,
            ) from exc

        original: bytes | None
        if action == "add":
            if path.exists():
                raise PatchError(
                    "target_exists",
                    f"cannot add file because it already exists: {relative_path}",
                    path=relative_path,
                )
            original = None
            text = ""
            text_format = _TextFormat(bom=False, newline="\n")
        else:
            if not path.is_file():
                raise PatchError(
                    "source_not_found",
                    f"source file does not exist: {relative_path}",
                    path=relative_path,
                )
            original = path.read_bytes()
            if len(original) > MAX_EDIT_BYTES:
                raise PatchError(
                    "file_too_large",
                    f"source file is {len(original)} bytes; limit is {MAX_EDIT_BYTES}",
                    path=relative_path,
                )
            text, text_format = _decode_text(original, relative_path)

        updated_text = _apply_hunks(text, patched_file, relative_path)
        if action == "delete":
            if updated_text:
                raise PatchError(
                    "invalid_delete",
                    "delete patch did not remove all file content",
                    path=relative_path,
                )
            updated = None
        else:
            updated = _encode_text(updated_text, text_format)
            if len(updated) > MAX_EDIT_BYTES:
                raise PatchError(
                    "content_too_large",
                    f"patched file is {len(updated)} bytes; limit is {MAX_EDIT_BYTES}",
                    path=relative_path,
                )

        outcome = PatchedFileOutcome(
            path=path.relative_to(self._boundary.root).as_posix(),
            action=action,
            hunks=len(patched_file),
            additions=int(patched_file.added),
            removals=int(patched_file.removed),
            old_hash=content_hash(original) if original is not None else None,
            new_hash=content_hash(updated) if updated is not None else None,
        )
        return FileMutation(path=path, original=original, updated=updated), outcome


# 解析 unified diff 并把第三方解析错误收敛成稳定 PatchError
def _parse_patch_set(patch_text: str) -> PatchSet:
    try:
        return PatchSet(patch_text.splitlines(keepends=True))
    except (UnidiffParseError, UnicodeDecodeError) as exc:
        raise PatchError("invalid_patch", f"invalid unified diff: {exc}") from exc


# 返回文件补丁的动作和规范化工作区相对路径
def _patch_file_identity(patched_file: Any) -> tuple[Action, str]:
    if patched_file.source_file == "/dev/null":
        return "add", _clean_patch_path(patched_file.target_file)
    if patched_file.target_file == "/dev/null":
        return "delete", _clean_patch_path(patched_file.source_file)
    source = _clean_patch_path(patched_file.source_file)
    target = _clean_patch_path(patched_file.target_file)
    if source != target:
        raise PatchError(
            "path_mismatch",
            f"source and target paths differ: {source!r} != {target!r}",
        )
    return "modify", source


# 按稳定 hunk id 重建仅含所选块的 unified diff
def _select_patch_hunks(plan: PatchPlan, selected: set[str]) -> str:
    patch_set = _parse_patch_set(plan.patch_text)
    planned_by_path = {item.path: item for item in plan.files}
    output: list[str] = []
    for patched_file in patch_set:
        _action, path = _patch_file_identity(patched_file)
        file_plan = planned_by_path[path]
        chosen = [
            (hunk_plan, hunk)
            for hunk_plan, hunk in zip(file_plan.hunks, patched_file, strict=True)
            if hunk_plan.id in selected
        ]
        if not chosen:
            continue
        if any(not hunk.selectable for hunk in file_plan.hunks):
            if len(chosen) != len(file_plan.hunks):
                raise PatchError(
                    "partial_file_action_not_supported",
                    f"all hunks must be selected for {file_plan.action} file: {path}",
                    path=path,
                )
        output.append(f"--- {patched_file.source_file}\n")
        output.append(f"+++ {patched_file.target_file}\n")
        output.extend(str(hunk) for _hunk_plan, hunk in chosen)
    return "".join(output)


def _clean_patch_path(raw_path: str) -> str:
    value = raw_path.strip().replace("\\", "/")
    if value in {"/dev/null", "dev/null"}:
        return value
    if value.startswith("a/") or value.startswith("b/"):
        value = value[2:]
    if not value or value.startswith('"') or "\x00" in value:
        raise PatchError("invalid_path", f"unsupported patch path: {raw_path!r}")
    return value


def _decode_text(raw: bytes, path: str) -> tuple[str, _TextFormat]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    payload = raw[3:] if bom else raw
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError("not_utf8", f"file is not valid UTF-8: {path}", path=path) from exc
    has_crlf = "\r\n" in text
    has_lone_lf = "\n" in text.replace("\r\n", "")
    if has_crlf and has_lone_lf:
        raise PatchError(
            "mixed_newlines",
            "files with mixed LF and CRLF newlines are not supported by apply_patch",
            path=path,
        )
    newline = "\r\n" if has_crlf else "\n"
    return text.replace("\r\n", "\n"), _TextFormat(bom=bom, newline=newline)


def _encode_text(text: str, text_format: _TextFormat) -> bytes:
    normalized = text.replace("\n", text_format.newline)
    encoded = normalized.encode("utf-8")
    return b"\xef\xbb\xbf" + encoded if text_format.bom else encoded


def _apply_hunks(original: str, patched_file: Any, path: str) -> str:
    source_lines = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk_number, hunk in enumerate(patched_file, start=1):
        start = (
            hunk.source_start
            if hunk.source_length == 0
            else max(hunk.source_start - 1, 0)
        )
        if start < cursor or start > len(source_lines):
            raise PatchError(
                "invalid_hunk_range",
                f"hunk {hunk_number} starts outside the available source range",
                path=path,
                hunk=hunk_number,
            )
        output.extend(source_lines[cursor:start])
        source_index = start
        consumed = 0
        produced = 0
        for line_type, value in _hunk_operations(hunk):
            if line_type in {" ", "-"}:
                actual = source_lines[source_index] if source_index < len(source_lines) else None
                if actual != value:
                    raise PatchError(
                        "hunk_mismatch",
                        f"hunk {hunk_number} does not match the current file",
                        path=path,
                        hunk=hunk_number,
                        line=source_index + 1,
                        expected=_display_line(value),
                        actual=_display_line(actual),
                    )
                source_index += 1
                consumed += 1
                if line_type == " ":
                    output.append(value)
                    produced += 1
            elif line_type == "+":
                output.append(value)
                produced += 1
        if consumed != hunk.source_length or produced != hunk.target_length:
            raise PatchError(
                "invalid_hunk_length",
                f"hunk {hunk_number} line counts do not match its header",
                path=path,
                hunk=hunk_number,
            )
        cursor = source_index
    output.extend(source_lines[cursor:])
    return "".join(output)


def _hunk_operations(hunk: Any) -> list[tuple[str, str]]:
    operations: list[tuple[str, str]] = []
    for line in hunk:
        if line.line_type == "\\":
            if not operations:
                raise PatchError("invalid_no_newline", "no-newline marker has no preceding line")
            line_type, value = operations[-1]
            operations[-1] = (line_type, value.removesuffix("\n"))
        else:
            operations.append((str(line.line_type), str(line.value)))
    return operations


def _display_line(value: str | None) -> str:
    if value is None:
        return "<end of file>"
    stripped = value.rstrip("\r\n")
    return stripped[:200] + ("..." if len(stripped) > 200 else "")

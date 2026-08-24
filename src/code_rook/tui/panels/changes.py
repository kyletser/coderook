from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, replace
from typing import Any, Literal

from rich.markup import escape
from textual import events
from textual.message import Message
from textual.widgets import Static

from code_rook.tui.product import tr

VerificationStatus = Literal["passed", "failed", "unavailable"]

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<section>.*)$"
)
_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
_PASS_STATUSES = {"ok", "pass", "passed", "success", "completed"}
_FAIL_STATUSES = {"error", "fail", "failed", "timeout", "truncated"}


@dataclass(frozen=True)
class ChangedFile:
    path: str
    original_path: str | None
    index_status: str
    worktree_status: str
    staged: bool
    unstaged: bool
    untracked: bool
    additions: int | None
    deletions: int | None
    review_status: str
    review_complete: bool
    review_note: str
    content_size: int | None
    content_sha256: str | None
    old_content_present: bool | None
    old_content_size: int | None
    old_content_sha256: str | None
    new_content_present: bool | None
    new_content_size: int | None
    new_content_sha256: str | None

    # 判断 Git XY 状态是否表示尚未解决的合并冲突
    @property
    def conflicted(self) -> bool:
        code = f"{self.index_status}{self.worktree_status}"
        return "U" in code or code in _CONFLICT_CODES

    # 返回适合文件列表展示的双字符 Git 状态
    @property
    def status_code(self) -> str:
        if self.untracked:
            return "??"
        return f"{self.index_status}{self.worktree_status}".replace(" ", "·")


@dataclass(frozen=True)
class ChangeHunk:
    file_path: str
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ChangeFileMetadata:
    file_path: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class VerificationEntry:
    name: str
    command: str | None
    status: VerificationStatus
    paths: tuple[str, ...]
    duration_ms: int | None
    failure: str | None


@dataclass(frozen=True)
class ChangeCenterSnapshot:
    scope: str
    files: tuple[ChangedFile, ...]
    hunks: tuple[ChangeHunk, ...]
    metadata: tuple[ChangeFileMetadata, ...]
    verifications: tuple[VerificationEntry, ...]
    additions: int
    deletions: int
    diff_truncated: bool
    state_digest: str
    verification_unavailable: bool

    # 返回所有存在未解决 Git 冲突的路径
    @property
    def conflict_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.conflicted)

    # 返回尚无成功验证证据覆盖的变更路径
    @property
    def unverified_paths(self) -> tuple[str, ...]:
        verified = {
            path
            for entry in self.verifications
            if entry.status == "passed"
            for path in entry.paths
        }
        return tuple(item.path for item in self.files if item.path not in verified)

    # 判断当前 receipt 是否包含任何失败验证
    @property
    def has_failed_verification(self) -> bool:
        return any(item.status == "failed" for item in self.verifications)

    # 返回正文或安全二进制摘要未完整进入审查快照的路径
    @property
    def unreviewable_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if not item.review_complete)

    # 只在无冲突、无失败且每个文件均有成功证据时声明验证完成
    @property
    def fully_verified(self) -> bool:
        return bool(self.files) and not (
            self.conflict_paths
            or self.has_failed_verification
            or self.verification_unavailable
            or self.unverified_paths
            or self.unreviewable_paths
        )


# 将 IPC 包装或裸 workspace.diff 结果统一收窄为 payload
def _unwrap_diff(payload: dict[str, Any]) -> dict[str, Any]:
    wrapped = payload.get("payload")
    if isinstance(wrapped, dict):
        return wrapped
    return payload


# 将 IPC 包装或裸 Turn Receipt 统一收窄为 receipt
def _unwrap_receipt(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    wrapped = payload.get("receipt")
    if isinstance(wrapped, dict):
        return wrapped
    return payload


# 将不可信整数字段解析为非负整数或未知值
def _optional_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


# 将 workspace.diff 文件数组解析为稳定排序的类型化记录
def _parse_files(payload: dict[str, Any]) -> tuple[ChangedFile, ...]:
    raw_files = payload.get("files", [])
    if not isinstance(raw_files, list):
        return ()
    files: list[ChangedFile] = []
    for raw in raw_files:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path", ""))
        if not path:
            continue
        original = raw.get("original_path")
        original_path = (
            str(original) if isinstance(original, str) else None
        )
        index_status = str(raw.get("index_status", " "))[:1] or " "
        worktree_status = str(raw.get("worktree_status", " "))[:1] or " "
        files.append(
            ChangedFile(
                path=path,
                original_path=original_path,
                index_status=index_status,
                worktree_status=worktree_status,
                staged=bool(raw.get("staged", False)),
                unstaged=bool(raw.get("unstaged", False)),
                untracked=bool(raw.get("untracked", False)),
                additions=_optional_count(raw.get("additions")),
                deletions=_optional_count(raw.get("deletions")),
                review_status=str(raw.get("review_status", "text")),
                review_complete=raw.get("review_complete", True) is True,
                review_note=str(raw.get("review_note", "")),
                content_size=_optional_count(raw.get("content_size")),
                content_sha256=(
                    str(raw["content_sha256"])
                    if isinstance(raw.get("content_sha256"), str)
                    else None
                ),
                old_content_present=(
                    raw.get("old_content_present")
                    if isinstance(raw.get("old_content_present"), bool)
                    else None
                ),
                old_content_size=_optional_count(raw.get("old_content_size")),
                old_content_sha256=(
                    str(raw["old_content_sha256"])
                    if isinstance(raw.get("old_content_sha256"), str)
                    else None
                ),
                new_content_present=(
                    raw.get("new_content_present")
                    if isinstance(raw.get("new_content_present"), bool)
                    else None
                ),
                new_content_size=_optional_count(raw.get("new_content_size")),
                new_content_sha256=(
                    str(raw["new_content_sha256"])
                    if isinstance(raw.get("new_content_sha256"), str)
                    else None
                ),
            )
        )
    return tuple(sorted(files, key=lambda item: (item.path.casefold(), item.path)))


# 将 Git 路径中的 C 风格引号安全解码为原始 UTF-8 路径
def _decode_git_quoted_path(value: str) -> str | None:
    if not (value.startswith('"') and value.endswith('"')):
        return None
    raw = bytearray()
    index = 1
    end = len(value) - 1
    escapes = {
        "a": 7,
        "b": 8,
        "t": 9,
        "n": 10,
        "v": 11,
        "f": 12,
        "r": 13,
        '"': 34,
        "\\": 92,
    }
    while index < end:
        character = value[index]
        if character != "\\":
            raw.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= end:
            return None
        escaped = value[index]
        if escaped in escapes:
            raw.append(escapes[escaped])
            index += 1
            continue
        if escaped in "01234567":
            digits = escaped
            index += 1
            while index < end and len(digits) < 3 and value[index] in "01234567":
                digits += value[index]
                index += 1
            raw.append(int(digits, 8))
            continue
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# 将路径编码为 Git 可能输出的强制 C 引号形式
def _git_quoted_path(value: str) -> str:
    escapes = {
        "\a": "\\a",
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\v": "\\v",
        "\f": "\\f",
        "\r": "\\r",
        '"': '\\"',
        "\\": "\\\\",
    }
    encoded: list[str] = []
    for character in value:
        replacement = escapes.get(character)
        if replacement is not None:
            encoded.append(replacement)
        elif ord(character) < 32 or ord(character) == 127:
            encoded.extend(f"\\{byte:03o}" for byte in character.encode("utf-8"))
        else:
            encoded.append(character)
    return '"' + "".join(encoded) + '"'


# 枚举原生 Git 与 CodeRook 合成补丁可能使用的等价路径 token
def _path_token_forms(prefix: str, path: str) -> tuple[str, ...]:
    value = f"{prefix}/{path}"
    return tuple(dict.fromkeys((value, _git_quoted_path(value), shlex.quote(value))))


# 用结构化文件清单精确解析 diff --git 的前后路径并拒绝歧义
def _known_path_from_diff_header(
    line: str,
    files: tuple[ChangedFile, ...],
) -> str:
    value = line[len("diff --git ") :]
    matches: set[str] = set()
    for item in files:
        old_paths = {item.path}
        if item.original_path:
            old_paths.add(item.original_path)
        for old_path in old_paths:
            for old_token in _path_token_forms("a", old_path):
                for new_token in _path_token_forms("b", item.path):
                    if value == f"{old_token} {new_token}":
                        matches.add(item.path)
    return next(iter(matches)) if len(matches) == 1 else ""


# 从 diff --git 行提取目标文件路径并优先绑定结构化文件清单
def _path_from_diff_header(
    line: str,
    files: tuple[ChangedFile, ...] = (),
) -> str:
    if files:
        return _known_path_from_diff_header(line, files)
    try:
        parts = shlex.split(line[len("diff --git ") :])
    except ValueError:
        return ""
    if len(parts) < 2:
        return ""
    path = parts[-1]
    return path[2:] if path.startswith("b/") else path


# 用结构化文件清单精确解析 --- 或 +++ 单路径文件头
def _known_path_from_file_header(
    value: str,
    files: tuple[ChangedFile, ...],
    *,
    old_side: bool,
) -> str:
    matches: set[str] = set()
    prefix = "a" if old_side else "b"
    for item in files:
        paths = {item.path}
        if old_side and item.original_path:
            paths.add(item.original_path)
        for path in paths:
            forms = _path_token_forms(prefix, path)
            if value in forms or (
                value.endswith("\t") and value[:-1] in forms
            ):
                matches.add(item.path if not old_side else path)
    return next(iter(matches)) if len(matches) == 1 else ""


# 从 --- 或 +++ 文件头提取规范化路径
def _path_from_file_header(
    line: str,
    files: tuple[ChangedFile, ...] = (),
) -> str:
    value = line[4:]
    if value == "/dev/null":
        return ""
    if files:
        return _known_path_from_file_header(
            value,
            files,
            old_side=line.startswith("--- "),
        )
    fallback_value = value[:-1] if value.endswith("\t") else value
    decoded = _decode_git_quoted_path(fallback_value)
    if decoded is not None:
        path = decoded
        return path[2:] if path.startswith(("a/", "b/")) else path
    try:
        parts = shlex.split(fallback_value)
    except ValueError:
        parts = []
    path = parts[0] if parts else fallback_value.strip('"')
    return path[2:] if path.startswith(("a/", "b/")) else path


# 解析统一 diff 的文件边界与 @@ hunk，未知元数据保持为普通 hunk 行
def parse_unified_diff(
    diff: str,
    files: tuple[ChangedFile, ...] = (),
) -> tuple[ChangeHunk, ...]:
    hunks: list[ChangeHunk] = []
    current_path = ""
    old_path = ""
    header = ""
    old_start = old_count = new_start = new_count = 0
    section = ""
    lines: list[str] = []

    # 将正在累积的 hunk 固化到结果列表
    def flush() -> None:
        nonlocal header, lines
        if not header:
            return
        hunks.append(
            ChangeHunk(
                file_path=current_path or old_path or "unknown",
                header=header,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                section=section,
                lines=tuple(lines),
            )
        )
        header = ""
        lines = []

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_path = _path_from_diff_header(line, files)
            old_path = ""
            continue
        if line.startswith("--- "):
            old_path = _path_from_file_header(line, files)
            continue
        if line.startswith("+++ "):
            candidate = _path_from_file_header(line, files)
            if candidate:
                current_path = candidate
            continue
        match = _HUNK_HEADER.match(line)
        if match is not None:
            flush()
            header = line
            old_start = int(match.group("old_start"))
            old_count = int(match.group("old_count") or "1")
            new_start = int(match.group("new_start"))
            new_count = int(match.group("new_count") or "1")
            section = match.group("section").strip()
            lines = [line]
            continue
        if header:
            lines.append(line)
    flush()
    return tuple(hunks)


# 逐文件保留 @@ 之外的 rename、mode、binary 与 blob 证据元数据块
def _parse_file_metadata(
    diff: str,
    files: tuple[ChangedFile, ...] = (),
) -> tuple[ChangeFileMetadata, ...]:
    records: list[ChangeFileMetadata] = []
    current_path = ""
    old_path = ""
    lines: list[str] = []
    inside_hunk = False

    # 固化当前文件的可见元数据，重复文件块按原顺序保留
    def flush() -> None:
        nonlocal lines
        if current_path or old_path:
            records.append(
                ChangeFileMetadata(
                    file_path=current_path or old_path,
                    lines=tuple(lines),
                )
            )
        lines = []

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            flush()
            current_path = _path_from_diff_header(line, files)
            old_path = ""
            lines = [line]
            inside_hunk = False
            continue
        if not current_path and not old_path:
            continue
        if line.startswith("--- "):
            old_path = _path_from_file_header(line, files)
        elif line.startswith("+++ "):
            candidate = _path_from_file_header(line, files)
            if candidate:
                current_path = candidate
        if _HUNK_HEADER.match(line) is not None:
            inside_hunk = True
            continue
        if not inside_hunk:
            lines.append(line)
    flush()
    return tuple(records)


# 将无法与任何可见 hunk 或元数据安全对齐的文本变更标记为审查阻断
def _fail_closed_unmatched_files(
    files: tuple[ChangedFile, ...],
    hunks: tuple[ChangeHunk, ...],
    metadata: tuple[ChangeFileMetadata, ...],
) -> tuple[ChangedFile, ...]:
    visible_paths = {
        *(hunk.file_path for hunk in hunks),
        *(record.file_path for record in metadata),
    }
    checked: list[ChangedFile] = []
    for item in files:
        has_visible_digest = item.review_status in {"binary", "opaque"} and bool(
            item.content_sha256 or item.old_content_sha256 or item.new_content_sha256
        )
        if not item.review_complete or item.path in visible_paths or has_visible_digest:
            checked.append(item)
            continue
        note = "Review blocked: visible diff could not be matched to this exact path"
        checked.append(
            replace(
                item,
                review_complete=False,
                review_note=f"{item.review_note}; {note}" if item.review_note else note,
            )
        )
    return tuple(checked)


# 将 verifier gate 的自由状态收窄为面板三态
def _verification_status(value: object, *, failed: bool = False) -> VerificationStatus:
    status = str(value or "").strip().casefold()
    if failed or status in _FAIL_STATUSES:
        return "failed"
    if status in _PASS_STATUSES:
        return "passed"
    return "unavailable"


# 将 receipt 中验证事件与 gate 展开为命令到结果的稳定映射
def _parse_verifications(receipt: dict[str, Any]) -> tuple[VerificationEntry, ...]:
    raw_entries = receipt.get("verification", [])
    if not isinstance(raw_entries, list):
        return ()
    entries: list[VerificationEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        raw_paths = raw.get("paths", [])
        paths = tuple(
            str(path).replace("\\", "/")
            for path in raw_paths
            if isinstance(path, str) and path
        ) if isinstance(raw_paths, list) else ()
        failure_value = raw.get("failure_class") or raw.get("error")
        failure = str(failure_value) if failure_value else None
        raw_gates = raw.get("gates", [])
        gates = (
            [gate for gate in raw_gates if isinstance(gate, dict)]
            if isinstance(raw_gates, list)
            else []
        )
        if gates:
            for gate in gates:
                command_value = gate.get("command")
                command = str(command_value) if command_value else None
                name = str(gate.get("name") or command or raw.get("action") or "verify")
                entries.append(
                    VerificationEntry(
                        name=name,
                        command=command,
                        status=_verification_status(
                            gate.get("status"),
                            failed=bool(raw.get("failed", 0)) and not gate.get("status"),
                        ),
                        paths=paths,
                        duration_ms=_optional_count(gate.get("duration_ms")),
                        failure=failure,
                    )
                )
            continue
        failed_count = _optional_count(raw.get("failed")) or 0
        status_value = raw.get("verdict", raw.get("status"))
        name = str(raw.get("action") or raw.get("tool") or "verification")
        entries.append(
            VerificationEntry(
                name=name,
                command=str(raw["command"]) if raw.get("command") else None,
                status=_verification_status(status_value, failed=failed_count > 0),
                paths=paths,
                duration_ms=_optional_count(raw.get("duration_ms")),
                failure=failure,
            )
        )
    return tuple(entries)


# 从 workspace.diff 与可选 receipt 构建无副作用的 Change Center 快照
def build_change_snapshot(
    diff_result: dict[str, Any],
    receipt_result: dict[str, Any] | None = None,
) -> ChangeCenterSnapshot:
    payload = _unwrap_diff(diff_result)
    receipt = _unwrap_receipt(receipt_result)
    unavailable = receipt.get("unavailable", [])
    verification_unavailable = not receipt or (
        isinstance(unavailable, list) and "verification" in unavailable
    )
    additions = _optional_count(payload.get("additions")) or 0
    deletions = _optional_count(payload.get("deletions")) or 0
    diff = str(payload.get("diff", ""))
    files = _parse_files(payload)
    hunks = parse_unified_diff(diff, files)
    metadata = _parse_file_metadata(diff, files)
    return ChangeCenterSnapshot(
        scope=str(payload.get("scope", "all")),
        files=_fail_closed_unmatched_files(files, hunks, metadata),
        hunks=hunks,
        metadata=metadata,
        verifications=_parse_verifications(receipt),
        additions=additions,
        deletions=deletions,
        diff_truncated=bool(payload.get("diff_truncated", False)),
        state_digest=str(payload.get("state_digest", "")),
        verification_unavailable=verification_unavailable,
    )


# 将一段普通文本按终端列宽截断并保留省略标记
def _fit(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1] + "…"


# 格式化未知或二进制文件的增删统计
def _file_stats(item: ChangedFile) -> str:
    additions = "?" if item.additions is None else str(item.additions)
    deletions = "?" if item.deletions is None else str(item.deletions)
    return f"+{additions} -{deletions}"


# 为单段 diff 内容应用最小化 Rich 颜色且转义仓库可控文本
def _render_diff_segment(segment: str, kind: str) -> str:
    fitted = escape(segment)
    if kind.startswith("@@"):
        return f"[cyan]{fitted}[/cyan]"
    if kind.startswith("+") and not kind.startswith("+++"):
        return f"[green]{fitted}[/green]"
    if kind.startswith("-") and not kind.startswith("---"):
        return f"[red]{fitted}[/red]"
    if kind.startswith("\\ No newline"):
        return f"[dim]{fitted}[/dim]"
    return fitted


# 将超长 diff 行完整折行，避免终端宽度裁剪导致正文无法审查
def _render_diff_rows(line: str, width: int) -> list[str]:
    safe_width = max(1, width)
    if not line:
        return [""]
    return [
        _render_diff_segment(line[offset : offset + safe_width], line)
        for offset in range(0, len(line), safe_width)
    ]


# 将 hunk 的全部逻辑行展开为可分页的终端行且不丢弃长行尾部
def _hunk_rows(hunk: ChangeHunk, width: int) -> list[str]:
    return [
        row
        for line in hunk.lines
        for row in _render_diff_rows(line, width)
    ]


# 将审查摘要按终端列宽完整折行，避免二进制哈希被省略
def _wrapped_text(value: str, width: int) -> list[str]:
    safe_width = max(1, width)
    if not value:
        return [""]
    return [
        value[offset : offset + safe_width]
        for offset in range(0, len(value), safe_width)
    ]


class ChangeCenterPanel:
    # 创建空面板；controller 随后可用 update 注入 IPC 结果
    def __init__(self) -> None:
        self._snapshot = build_change_snapshot({})
        self._file_index: int | None = None
        self._hunk_index: int | None = None
        self._line_offset = 0
        self._last_hunk_budget = 1

    # 返回当前不可变事实快照供 controller 决定 stage/review 行为
    @property
    def snapshot(self) -> ChangeCenterSnapshot:
        return self._snapshot

    # 返回当前选中文件，没有变更时返回空值
    @property
    def current_file(self) -> ChangedFile | None:
        if self._file_index is None:
            return None
        return self._snapshot.files[self._file_index]

    # 返回当前选中 hunk，文件没有文本 hunk 时返回空值
    @property
    def current_hunk(self) -> ChangeHunk | None:
        if self._hunk_index is None:
            return None
        return self._snapshot.hunks[self._hunk_index]

    # 返回当前文件所有 Git 元数据块并保留原始出现顺序
    @property
    def current_metadata(self) -> tuple[ChangeFileMetadata, ...]:
        current = self.current_file
        if current is None:
            return ()
        paths = {current.path}
        if current.original_path:
            paths.add(current.original_path)
        return tuple(
            item for item in self._snapshot.metadata if item.file_path in paths
        )

    # 用最新 diff 和 receipt 原子替换快照并尽量保留原文件选择
    def update(
        self,
        diff_result: dict[str, Any],
        receipt_result: dict[str, Any] | None = None,
    ) -> None:
        previous_path = self.current_file.path if self.current_file is not None else None
        self._snapshot = build_change_snapshot(diff_result, receipt_result)
        self._file_index = 0 if self._snapshot.files else None
        self._line_offset = 0
        if previous_path is not None:
            self.select_file(previous_path)
        else:
            self._sync_hunk_to_file()

    # 按索引或路径选择文件并跳到该文件首个 hunk
    def select_file(self, target: int | str) -> bool:
        if isinstance(target, int):
            index = target
        else:
            index = next(
                (
                    position
                    for position, item in enumerate(self._snapshot.files)
                    if item.path == target
                ),
                -1,
            )
        if not 0 <= index < len(self._snapshot.files):
            return False
        changed = index != self._file_index
        self._file_index = index
        self._line_offset = 0
        self._sync_hunk_to_file()
        return changed

    # 将文件选择向前移动一步且不在边界循环
    def previous_file(self) -> bool:
        if self._file_index is None or self._file_index <= 0:
            return False
        return self.select_file(self._file_index - 1)

    # 将文件选择向后移动一步且不在边界循环
    def next_file(self) -> bool:
        if self._file_index is None or self._file_index >= len(self._snapshot.files) - 1:
            return False
        return self.select_file(self._file_index + 1)

    # 将 hunk 选择向前移动并同步其所属文件
    def previous_hunk(self) -> bool:
        return self._move_hunk(-1)

    # 将 hunk 选择向后移动并同步其所属文件
    def next_hunk(self) -> bool:
        return self._move_hunk(1)

    # 将当前 hunk 的可见正文向前翻一页且不越过开头
    def previous_page(self) -> bool:
        if self._line_offset <= 0:
            return False
        self._line_offset = max(0, self._line_offset - self._last_hunk_budget)
        return True

    # 将当前 hunk 的可见正文向后翻一页，末页边界由最近一次渲染固定
    def next_page(self) -> bool:
        rows = self._current_diff_rows(self._last_render_width())
        target = self._line_offset + self._last_hunk_budget
        if target >= len(rows):
            return False
        self._line_offset = target
        return True

    # 返回最近一次渲染正文采用的安全列宽
    def _last_render_width(self) -> int:
        return max(1, getattr(self, "_hunk_width", 80))

    # 展开当前文件元数据和当前 hunk，供渲染与分页消费同一事实序列
    def _current_diff_rows(self, width: int) -> list[str]:
        logical_lines = [
            line
            for metadata in self.current_metadata
            for line in metadata.lines
        ]
        hunk = self.current_hunk
        if hunk is not None:
            logical_lines.extend(hunk.lines)
        return [
            row
            for line in logical_lines
            for row in _render_diff_rows(line, width)
        ]

    # 在全局 hunk 序列内执行有界移动并同步文件索引
    def _move_hunk(self, delta: int) -> bool:
        if not self._snapshot.hunks:
            return False
        current = self._hunk_index if self._hunk_index is not None else 0
        target = current + delta
        if not 0 <= target < len(self._snapshot.hunks):
            return False
        self._hunk_index = target
        self._line_offset = 0
        path = self._snapshot.hunks[target].file_path
        for index, item in enumerate(self._snapshot.files):
            if item.path == path:
                self._file_index = index
                break
        return True

    # 把 hunk 选择定位到当前文件第一个文本块，无文本差异时清空选择
    def _sync_hunk_to_file(self) -> None:
        current = self.current_file
        if current is None:
            self._hunk_index = None
            self._line_offset = 0
            return
        self._hunk_index = next(
            (
                index
                for index, hunk in enumerate(self._snapshot.hunks)
                if hunk.file_path == current.path
            ),
            None,
        )
        self._line_offset = 0

    # 渲染安全状态、文件、当前 hunk 与验证映射，按终端尺寸收缩内容预算
    def render(
        self,
        *,
        width: int = 100,
        height: int = 30,
        locale: str = "en-US",
    ) -> str:
        width = max(40, width)
        height = max(12, height)
        snapshot = self._snapshot
        lines = [
            f"[bold cyan]{escape(tr('changes.title', locale))}[/bold cyan]  "
            f"[dim]{escape(snapshot.scope)} · "
            f"{escape(tr('changes.file_count', locale, count=len(snapshot.files)))} · "
            f"[green]+{snapshot.additions}[/green] [red]-{snapshot.deletions}[/red][/dim]"
        ]
        lines.extend(self._render_warnings(width, locale=locale))
        if not snapshot.files:
            lines.append(f"[dim]{escape(tr('changes.empty', locale))}[/dim]")
            return "\n".join(lines)

        compact = width < 100 or height < 28
        file_budget = min(len(snapshot.files), 4 if compact else 8)
        lines.append(
            f"[bold]{escape(tr('changes.files', locale))}[/bold]  "
            f"[dim]{escape(tr('changes.navigation', locale))}[/dim]"
        )
        for index, item in enumerate(snapshot.files[:file_budget]):
            marker = "[cyan]›[/cyan]" if index == self._file_index else " "
            conflict = (
                f" [red]{escape(tr('changes.conflict', locale))}[/red]"
                if item.conflicted
                else ""
            )
            status = escape(item.status_code)
            stats = escape(_file_stats(item))
            review = (
                " [red]review blocked[/red]"
                if not item.review_complete
                else (
                    " [yellow]opaque[/yellow]"
                    if item.review_status in {"binary", "opaque"}
                    else ""
                )
            )
            reserved = 12 + len(item.status_code) + len(_file_stats(item)) + len(review)
            path = escape(_fit(item.path, max(12, width - reserved)))
            lines.append(
                f"{marker} [dim]{status}[/dim] {path}  [dim]{stats}[/dim]"
                f"{review}{conflict}"
            )
        if len(snapshot.files) > file_budget:
            lines.append(
                "[dim]  "
                + escape(
                    tr(
                        "changes.more_files",
                        locale,
                        count=len(snapshot.files) - file_budget,
                    )
                )
                + "[/dim]"
            )

        current = self.current_file
        hunk = self.current_hunk
        title_path = escape(
            _fit(current.path if current is not None else "-", max(12, width - 28))
        )
        if hunk is not None:
            position = (self._hunk_index or 0) + 1
            lines.append(
                f"[bold]Diff · {title_path}[/bold]  "
                "[dim]"
                + escape(
                    tr(
                        "changes.hunk_position",
                        locale,
                        position=position,
                        total=len(snapshot.hunks),
                    )
                )
                + "[/dim]"
            )
        else:
            lines.append(f"[bold]Diff · {title_path}[/bold]")

        verification_reserve = 4 if snapshot.verifications else 2
        remaining = max(3, height - len(lines) - verification_reserve)
        self._hunk_width = width
        rows = self._current_diff_rows(width)
        if rows:
            row_budget = min(len(rows), remaining)
            self._last_hunk_budget = max(1, row_budget)
            max_offset = max(0, len(rows) - 1)
            self._line_offset = min(self._line_offset, max_offset)
            visible_rows = rows[self._line_offset : self._line_offset + row_budget]
            lines.extend(visible_rows)
            hidden = max(0, len(rows) - self._line_offset - len(visible_rows))
            if hidden:
                lines.append(
                    "[dim]"
                    + escape(
                        tr(
                            "changes.more_hunk_lines",
                            locale,
                            count=hidden,
                        )
                    )
                    + "[/dim]"
                )
        elif current is not None and current.review_note:
            color = "red" if not current.review_complete else "yellow"
            lines.extend(
                f"[{color}]{escape(row)}[/{color}]"
                for row in _wrapped_text(current.review_note, width)
            )
        if hunk is None:
            lines.append(f"[dim]{escape(tr('changes.no_hunk', locale))}[/dim]")

        lines.extend(self._render_verifications(width, compact=compact, locale=locale))
        return "\n".join(lines[:height])

    # 渲染冲突、截断和缺少验证证据的阻断提示
    def _render_warnings(self, width: int, *, locale: str) -> list[str]:
        snapshot = self._snapshot
        warnings: list[str] = []
        if snapshot.conflict_paths:
            paths = ", ".join(snapshot.conflict_paths)
            warnings.append(
                f"[bold red]{escape(tr('changes.conflicts_block', locale))}[/bold red] "
                f"{escape(_fit(paths, max(12, width - 30)))}"
            )
        if snapshot.files:
            if snapshot.has_failed_verification:
                warnings.append(
                    f"[bold red]{escape(tr('changes.verification_failed', locale))}[/bold red]"
                )
            elif snapshot.verification_unavailable:
                warnings.append(
                    f"[yellow]{escape(tr('changes.verification_unavailable', locale))}[/yellow]"
                )
            elif snapshot.unverified_paths:
                count = len(snapshot.unverified_paths)
                warnings.append(
                    "[yellow]"
                    + escape(tr("changes.unverified_count", locale, count=count))
                    + "[/yellow]"
                )
            elif snapshot.fully_verified:
                warnings.append(
                    f"[green]{escape(tr('changes.fully_verified', locale))}[/green]"
                )
        if snapshot.diff_truncated:
            warnings.append(
                f"[yellow]{escape(tr('changes.diff_truncated', locale))}[/yellow]"
            )
        if snapshot.unreviewable_paths:
            paths = ", ".join(snapshot.unreviewable_paths)
            warnings.append(
                "[bold red]Visible review required before stage/commit:[/bold red] "
                + escape(_fit(paths, max(12, width - 46)))
            )
        return warnings

    # 渲染验证命令、状态、耗时与作用路径的紧凑映射
    def _render_verifications(
        self,
        width: int,
        *,
        compact: bool,
        locale: str,
    ) -> list[str]:
        entries = self._snapshot.verifications
        if not entries:
            return [
                f"[bold]{escape(tr('changes.verification', locale))}[/bold]  "
                f"[dim]{escape(tr('changes.none_recorded', locale))}[/dim]"
            ]
        lines = [
            f"[bold]{escape(tr('changes.verification', locale))}[/bold]  "
            f"[dim]{escape(tr('changes.verification_mapping', locale))}[/dim]"
        ]
        budget = 2 if compact else 5
        markers = {
            "passed": "[green]✓[/green]",
            "failed": "[red]×[/red]",
            "unavailable": "[yellow]?[/yellow]",
        }
        for entry in entries[:budget]:
            command = entry.command or entry.name
            paths = ", ".join(entry.paths) or tr("changes.scope_unavailable", locale)
            duration = f" · {entry.duration_ms}ms" if entry.duration_ms is not None else ""
            content = f"{command} → {entry.status}{duration} → {paths}"
            lines.append(f"  {markers[entry.status]} {escape(_fit(content, max(12, width - 4)))}")
        if len(entries) > budget:
            lines.append(
                "[dim]  "
                + escape(
                    tr(
                        "changes.more_verifications",
                        locale,
                        count=len(entries) - budget,
                    )
                )
                + "[/dim]"
            )
        return lines


class ChangeCenterOverlay(Static):
    """覆盖主时间线的全屏、可聚焦 Change Center。"""

    can_focus = True

    DEFAULT_CSS = """
    ChangeCenterOverlay {
        layer: overlay;
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: solid #4d8994;
        background: #111419;
        color: $text;
    }
    """

    class Dismissed(Message):
        # 初始化 Change Center 关闭消息
        def __init__(self, overlay: ChangeCenterOverlay) -> None:
            self.overlay = overlay
            super().__init__()

    # 初始化面板快照、语言与首屏内容
    def __init__(
        self,
        diff_result: dict[str, Any],
        receipt_result: dict[str, Any] | None = None,
        *,
        locale: str = "zh-CN",
    ) -> None:
        super().__init__(classes="change-center-overlay")
        self.panel = ChangeCenterPanel()
        self.panel.update(diff_result, receipt_result)
        self._locale = locale

    # 挂载后抢占焦点并按真实可用尺寸渲染
    def on_mount(self) -> None:
        self.focus()
        self._refresh()

    # 尺寸变化时重新分配文件、hunk 与验证区域预算
    def on_resize(self, _event: events.Resize) -> None:
        self._refresh()

    # 切换语言后立即刷新面板标题、风险和键盘提示
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        self._refresh()

    # 使用当前控件尺寸刷新安全 Rich 文本
    def _refresh(self) -> None:
        width = max(40, self.size.width - 4)
        height = max(12, self.size.height - 3)
        content = self.panel.render(width=width, height=height, locale=self._locale)
        hint = escape(tr("changes.close_hint", self._locale))
        self.update(f"{content}\n[dim]{hint}[/dim]")

    # 处理文件、hunk 导航和 Esc 关闭
    def on_key(self, event: events.Key) -> None:
        changed = False
        if event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))
            return
        if event.key in {"up", "k"}:
            event.stop()
            changed = self.panel.previous_file()
        elif event.key in {"down", "j"}:
            event.stop()
            changed = self.panel.next_file()
        elif event.key in {"left", "p"}:
            event.stop()
            changed = self.panel.previous_hunk()
        elif event.key in {"right", "n"}:
            event.stop()
            changed = self.panel.next_hunk()
        elif event.key == "pageup":
            event.stop()
            changed = self.panel.previous_page()
        elif event.key == "pagedown":
            event.stop()
            changed = self.panel.next_page()
        if changed:
            self._refresh()

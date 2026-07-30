from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kyle_claude.core.session.model import Session

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]
_SESSION_ID_RE = re.compile(r"^sess-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class IncompleteToolCall:
    run_id: str
    tool_use_id: str
    tool_name: str
    step: int


@dataclass(frozen=True)
class TranscriptRecovery:
    archive_path: Path
    run_ids: tuple[str, ...]
    tool_use_ids: tuple[str, ...]
    kept_rows: int
    discarded_rows: int


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionStore:
    # 初始化 session 文件存储根目录
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser()
        self._root.mkdir(parents=True, exist_ok=True)
        self._known_block_ids: dict[str, set[str]] = {}
        self._cleanup_deleted_sessions()

    def _cleanup_deleted_sessions(self) -> None:
        for tombstone in self._root.glob(".deleted-sess-*"):
            try:
                shutil.rmtree(tombstone)
            except OSError:
                logger.warning("could not clean deleted session: %s", tombstone, exc_info=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if _SESSION_ID_RE.fullmatch(sid) is None:
            raise ValueError(f"invalid session id: {sid!r}")
        return self._root / sid

    # 返回指定 session 下的 runs 目录路径
    def runs_dir(self, sid: str) -> Path:
        return self.session_dir(sid) / "runs"

    # 将 session meta 写入 meta.json
    def write_meta(self, session: Session) -> None:
        path = self.session_dir(session.id)
        path.mkdir(parents=True, exist_ok=True)
        self._replace_file(
            path / "meta.json",
            (json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    def create_fork(self, source_sid: str, session: Session) -> None:
        source = self.session_dir(source_sid)
        if not (source / "meta.json").is_file():
            raise FileNotFoundError(f"source session does not exist: {source_sid}")
        destination = self.session_dir(session.id)
        if destination.exists():
            raise FileExistsError(f"destination session already exists: {session.id}")

        temp_dir = Path(
            tempfile.mkdtemp(prefix=f".fork-{session.id}-", dir=self._root)
        )
        try:
            for filename in ("thread.jsonl", "notes.md"):
                source_file = source / filename
                if source_file.is_file():
                    self._write_new_file(temp_dir / filename, source_file.read_bytes())
            meta = json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n"
            self._write_new_file(temp_dir / "meta.json", meta.encode("utf-8"))
            os.replace(temp_dir, destination)
            self._fsync_directory(self._root)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)

    def delete_session(self, sid: str) -> None:
        source = self.session_dir(sid)
        if not source.exists():
            raise FileNotFoundError(f"session does not exist: {sid}")
        tombstone = self._root / f".deleted-{sid}-{uuid.uuid4().hex[:8]}"
        os.replace(source, tombstone)
        self._fsync_directory(self._root)
        self._known_block_ids.pop(sid, None)
        try:
            shutil.rmtree(tombstone)
        except OSError:
            logger.warning("session tombstone cleanup deferred: %s", tombstone, exc_info=True)

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        data = json.loads((self.session_dir(sid) / "meta.json").read_text(encoding="utf-8"))
        return Session.from_dict(data)

    # 扫描持久化目录，跳过损坏条目并按最近更新时间倒序返回
    def list_sessions(self) -> list[Session]:
        sessions: list[Session] = []
        for meta_path in self._root.glob("sess-*/meta.json"):
            sid = meta_path.parent.name
            try:
                sessions.append(self.read_meta(sid))
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                logger.warning("skip invalid session metadata: %s", meta_path, exc_info=True)
        return sorted(sessions, key=lambda session: session.updated_at, reverse=True)

    # 追加一条 Anthropic API 消息到 thread.jsonl
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        row: dict[str, Any] = {
            "schema_version": 2,
            "kind": "message",
            "ts": _now(),
            "role": role,
            "content": content,
        }
        if run_id is not None:
            row["run_id"] = run_id
        if message_id is not None:
            row["message_id"] = message_id
        self._append_jsonl(self.session_dir(sid) / "thread.jsonl", row)

    # 批量追加一次 run 新产生的消息到 thread.jsonl
    def append_messages(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        for msg in messages:
            self.append_message(
                sid,
                role=str(msg["role"]),
                content=msg["content"],
                run_id=run_id,
            )

    def append_block(
        self,
        sid: str,
        *,
        role: str,
        block: dict[str, Any],
        run_id: str,
        step: int,
        message_id: str,
        block_id: str,
        block_index: int,
        block_count: int,
    ) -> bool:
        known_ids = self._block_ids(sid)
        if block_id in known_ids:
            return False
        row: dict[str, Any] = {
            "schema_version": 2,
            "kind": "block",
            "ts": _now(),
            "role": role,
            "block": block,
            "run_id": run_id,
            "step": step,
            "message_id": message_id,
            "block_id": block_id,
            "block_index": block_index,
            "block_count": block_count,
        }
        self._append_jsonl(self.session_dir(sid) / "thread.jsonl", row)
        known_ids.add(block_id)
        return True

    # 读取完整 thread 并返回可直接传给 Anthropic 的 messages
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []

        messages: list[dict[str, Any]] = []
        last_message_id: str | None = None
        seen_block_ids: set[str] = set()
        for line_no, row in self._read_rows(sid):
            role = row.get("role")
            if role not in ("user", "assistant"):
                logger.warning(
                    "skip unknown thread role sid=%s line=%s role=%s",
                    sid,
                    line_no,
                    role,
                )
                continue
            if row.get("kind") == "block":
                block = row.get("block")
                block_id = str(row.get("block_id", ""))
                message_id = str(row.get("message_id", ""))
                if not isinstance(block, dict) or not block_id or not message_id:
                    logger.warning("skip invalid thread block sid=%s line=%s", sid, line_no)
                    continue
                if block_id in seen_block_ids:
                    logger.warning("skip duplicate thread block sid=%s block=%s", sid, block_id)
                    continue
                seen_block_ids.add(block_id)
                if (
                    messages
                    and last_message_id == message_id
                    and messages[-1]["role"] == role
                    and isinstance(messages[-1]["content"], list)
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": role, "content": [block]})
                last_message_id = message_id
                continue

            messages.append({"role": role, "content": row.get("content", "")})
            last_message_id = str(row.get("message_id", "")) or None

        messages = self._trim_orphan_tool_use(messages)
        from kyle_claude.core.compact.budget import truncate_tool_results
        return truncate_tool_results(messages)

    def find_incomplete_tool_calls(self, sid: str) -> list[IncompleteToolCall]:
        _, pending, _, _ = self._scan_recovery_state(sid)
        return list(pending.values())

    def recover_incomplete_tail(self, sid: str) -> TranscriptRecovery | None:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return None
        raw = path.read_bytes()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        kept_rows, pending, damaged, incomplete_run_ids = self._scan_recovery_state(
            sid,
            lines=lines,
        )
        if not damaged and not pending:
            return None

        run_ids = tuple(sorted(incomplete_run_ids))
        tool_use_ids = tuple(sorted(pending))
        suffix = run_ids[-1] if run_ids else "unknown"
        archive = self.session_dir(sid) / (
            f"thread_interrupted_{suffix}_{uuid.uuid4().hex[:8]}.jsonl"
        )
        self._write_new_file(archive, raw)

        retained = "\n".join(lines[:kept_rows])
        retained_bytes = (retained + "\n").encode("utf-8") if retained else b""
        self._replace_file(path, retained_bytes)
        self._known_block_ids.pop(sid, None)

        recovery = TranscriptRecovery(
            archive_path=archive,
            run_ids=run_ids,
            tool_use_ids=tool_use_ids,
            kept_rows=kept_rows,
            discarded_rows=max(0, len(lines) - kept_rows),
        )
        self._append_jsonl(
            self.session_dir(sid) / "transcript_recoveries.jsonl",
            {
                "schema_version": 1,
                "ts": _now(),
                "action": "trim_to_last_balanced_message",
                "archive": archive.name,
                "run_ids": list(run_ids),
                "tool_use_ids": list(tool_use_ids),
                "kept_rows": recovery.kept_rows,
                "discarded_rows": recovery.discarded_rows,
            },
        )
        logger.warning(
            "recovered interrupted transcript sid=%s runs=%s tools=%s archive=%s",
            sid,
            run_ids,
            tool_use_ids,
            archive,
        )
        return recovery

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    def _read_rows(self, sid: str) -> list[tuple[int, dict[str, Any]]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.exists():
            return []
        rows: list[tuple[int, dict[str, Any]]] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip broken thread row sid=%s line=%s", sid, line_no)
                continue
            if not isinstance(row, dict):
                logger.warning("skip non-object thread row sid=%s line=%s", sid, line_no)
                continue
            rows.append((line_no, row))
        return rows

    def _block_ids(self, sid: str) -> set[str]:
        cached = self._known_block_ids.get(sid)
        if cached is not None:
            return cached
        block_ids = {
            str(row["block_id"])
            for _, row in self._read_rows(sid)
            if row.get("kind") == "block" and row.get("block_id")
        }
        self._known_block_ids[sid] = block_ids
        return block_ids

    def _scan_recovery_state(
        self,
        sid: str,
        *,
        lines: list[str] | None = None,
    ) -> tuple[int, dict[str, IncompleteToolCall], bool, set[str]]:
        if lines is None:
            path = self.session_dir(sid) / "thread.jsonl"
            if not path.exists():
                return 0, {}, False, set()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

        pending: dict[str, IncompleteToolCall] = {}
        pending_starts: dict[str, int] = {}
        seen_block_ids: set[str] = set()
        message_starts: dict[str, int] = {}
        message_run_ids: dict[str, str] = {}
        message_groups: dict[str, tuple[int, int, set[int]]] = {}
        last_balanced = 0
        damaged = False
        for index, line in enumerate(lines, start=1):
            if not line:
                if not pending and not damaged:
                    last_balanced = index
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                damaged = True
                continue
            if not isinstance(row, dict):
                damaged = True
                continue

            blocks: list[dict[str, Any]] = []
            if row.get("kind") == "block":
                block_id = str(row.get("block_id", ""))
                message_id = str(row.get("message_id", ""))
                block = row.get("block")
                block_index = row.get("block_index")
                block_count = row.get("block_count")
                if (
                    not block_id
                    or not message_id
                    or not isinstance(block, dict)
                    or not isinstance(block_index, int)
                    or not isinstance(block_count, int)
                    or block_count < 1
                    or block_index < 0
                    or block_index >= block_count
                ):
                    damaged = True
                    continue
                if block_id in seen_block_ids:
                    continue
                seen_block_ids.add(block_id)
                message_start = message_starts.setdefault(message_id, index)
                message_run_ids.setdefault(message_id, str(row.get("run_id", "")))
                group_start, expected_count, indexes = message_groups.setdefault(
                    message_id,
                    (message_start, block_count, set()),
                )
                if expected_count != block_count or block_index in indexes:
                    damaged = True
                    continue
                indexes.add(block_index)
                message_groups[message_id] = (group_start, expected_count, indexes)
                blocks.append(block)
            else:
                content = row.get("content")
                if isinstance(content, list):
                    blocks.extend(block for block in content if isinstance(block, dict))

            role = row.get("role")
            for block in blocks:
                if role == "assistant" and block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id", ""))
                    if not tool_use_id:
                        damaged = True
                        continue
                    raw_step = row.get("step", 0)
                    step = raw_step if isinstance(raw_step, int) else 0
                    pending[tool_use_id] = IncompleteToolCall(
                        run_id=str(row.get("run_id", "")),
                        tool_use_id=tool_use_id,
                        tool_name=str(block.get("name", "")),
                        step=step,
                    )
                    message_id = str(row.get("message_id", ""))
                    pending_starts[tool_use_id] = message_starts.get(message_id, index) - 1
                elif role == "user" and block.get("type") == "tool_result":
                    tool_use_id = str(block.get("tool_use_id", ""))
                    pending.pop(tool_use_id, None)
                    pending_starts.pop(tool_use_id, None)
            if not pending and not damaged:
                last_balanced = index

        incomplete_group_starts = [
            start - 1
            for start, expected_count, indexes in message_groups.values()
            if len(indexes) != expected_count
        ]
        if incomplete_group_starts:
            damaged = True
            last_balanced = min(last_balanced, *incomplete_group_starts)
        if pending_starts:
            last_balanced = min(last_balanced, *pending_starts.values())
        incomplete_message_ids = {
            message_id
            for message_id, (_, expected_count, indexes) in message_groups.items()
            if len(indexes) != expected_count
        }
        incomplete_run_ids = {
            run_id
            for message_id in incomplete_message_ids
            if (run_id := message_run_ids.get(message_id, ""))
        }
        incomplete_run_ids.update(call.run_id for call in pending.values() if call.run_id)
        return last_balanced, pending, damaged, incomplete_run_ids

    def _append_jsonl(self, path: Path, row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as file:
            file.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def _write_new_file(self, path: Path, content: bytes) -> None:
        with path.open("xb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        self._fsync_directory(path.parent)

    def _replace_file(self, path: Path, content: bytes) -> None:
        descriptor, raw_temp = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
            self._fsync_directory(path.parent)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    # 将压缩后的消息对覆盖写入 thread.jsonl，原文件备份为 thread_<ts>.jsonl.bak
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        path = self.session_dir(sid) / "thread.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        bak = self.session_dir(sid) / f"thread_{ts_str}.jsonl.bak"
        if path.exists():
            self._write_new_file(bak, path.read_bytes())
        rows = [
            {
                "schema_version": 2,
                "kind": "message",
                "ts": _now(),
                "role": msg["role"],
                "content": msg["content"],
            }
            for msg in messages
        ]
        encoded = "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ).encode("utf-8")
        self._replace_file(path, encoded)
        self._known_block_ids.pop(sid, None)

    # 读取 notes.md 全文，文件不存在时返回空字符串
    def read_notes(self, sid: str) -> str:
        path = self.session_dir(sid) / "notes.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    # 将一条主动笔记追加到 notes.md
    def append_note(self, sid: str, content: str, run_id: str) -> None:
        path = self.session_dir(sid)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "notes.md").open("a", encoding="utf-8") as f:
            f.write(f"## Note ({_now()}, {run_id})\n{content}\n\n")


class SessionTranscriptSink:
    def __init__(self, store: SessionStore, session_id: str, run_id: str) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id

    def append_assistant(self, step: int, blocks: list[dict[str, object]]) -> None:
        message_id = f"{self._run_id}:assistant:{step}"
        for index, block in enumerate(blocks):
            self._store.append_block(
                self._session_id,
                role="assistant",
                block=dict(block),
                run_id=self._run_id,
                step=step,
                message_id=message_id,
                block_id=f"{message_id}:{index}",
                block_index=index,
                block_count=len(blocks),
            )

    # 持久化循环主动注入的普通用户消息，避免伪装成无对应 tool_use 的 tool_result
    def append_user(self, step: int, content: str) -> None:
        self._store.append_message(
            self._session_id,
            role="user",
            content=content,
            run_id=self._run_id,
            message_id=f"{self._run_id}:user:{step}",
        )

    def append_tool_result(
        self,
        step: int,
        tool_use_id: str,
        content: str,
        *,
        is_error: bool,
        block_index: int,
        block_count: int,
    ) -> None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        message_id = f"{self._run_id}:tool-results:{step}"
        self._store.append_block(
            self._session_id,
            role="user",
            block=block,
            run_id=self._run_id,
            step=step,
            message_id=message_id,
            block_id=f"{message_id}:{tool_use_id}",
            block_index=block_index,
            block_count=block_count,
        )

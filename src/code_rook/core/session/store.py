from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from code_rook.core.execution.invariants import (
    InvariantViolation,
    validate_session_events,
)
from code_rook.core.execution.models import RequestSnapshot, SessionEventEnvelope
from code_rook.core.quarantine import quarantine_invalid_file
from code_rook.core.session.model import (
    SESSION_ID_PATTERN,
    Session,
    UnsupportedSessionSchemaError,
)

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]


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
    def __init__(self, root: Path, *, initialize: bool = True) -> None:
        self._root = root.expanduser().absolute()
        if self._root.is_symlink() or (
            self._root.exists() and not self._root.is_dir()
        ):
            raise ValueError("session state root must be a real directory")
        self._known_block_ids: dict[str, set[str]] = {}
        self._ledger_heads: dict[str, tuple[int, str]] = {}
        self._ledger_locks: dict[str, threading.RLock] = {}
        self._ledger_locks_guard = threading.Lock()
        if initialize:
            self._root.mkdir(parents=True, exist_ok=True)
            self._cleanup_deleted_sessions()

    @property
    # 返回 session 状态根目录，供只读一致性检查限定扫描边界
    def root(self) -> Path:
        return self._root

    def _cleanup_deleted_sessions(self) -> None:
        for tombstone in self._root.glob(".deleted-sess-*"):
            try:
                shutil.rmtree(tombstone)
            except OSError:
                logger.warning("could not clean deleted session: %s", tombstone, exc_info=True)

    # 返回指定 session 的目录路径
    def session_dir(self, sid: str) -> Path:
        if SESSION_ID_PATTERN.fullmatch(sid) is None:
            raise ValueError(f"invalid session id: {sid!r}")
        path = self._root / sid
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"unsafe session state path: {sid!r}")
        if path.exists() and self._root.exists() and not path.resolve().is_relative_to(
            self._root.resolve()
        ):
            raise ValueError(f"session state path crosses root: {sid!r}")
        return path

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
        self._ledger_heads.pop(sid, None)
        try:
            shutil.rmtree(tombstone)
        except OSError:
            logger.warning("session tombstone cleanup deferred: %s", tombstone, exc_info=True)

    # 从 meta.json 读取 session meta
    def read_meta(self, sid: str) -> Session:
        meta = self.session_dir(sid) / "meta.json"
        if meta.is_symlink() or not meta.is_file():
            raise ValueError("session metadata must be a real file")
        data = json.loads(meta.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("session metadata must be an object")
        session = Session.from_dict(data)
        if session.id != sid:
            raise ValueError("session id does not match its directory")
        return session

    # 扫描持久化目录，隔离损坏元数据并按最近更新时间倒序返回
    def list_sessions(self, *, quarantine_invalid: bool = True) -> list[Session]:
        sessions: list[Session] = []
        for meta_path in self._root.glob("sess-*/meta.json"):
            sid = meta_path.parent.name
            try:
                sessions.append(self.read_meta(sid))
            except UnsupportedSessionSchemaError:
                logger.warning("skip unsupported future session metadata: %s", meta_path)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                quarantined = (
                    quarantine_invalid_file(
                        meta_path,
                        category="session",
                        reason="record failed strict Session validation",
                        state_root=self._root,
                    )
                    if quarantine_invalid
                    else None
                )
                logger.warning(
                    "%s invalid session metadata: %s",
                    "isolated" if quarantine_invalid else "skipped",
                    quarantined or meta_path,
                    exc_info=True,
                )
        return sorted(sessions, key=lambda session: session.updated_at, reverse=True)

    # 将一条模型可见消息直接追加为唯一的 v2 SessionEvent
    def append_message(
        self,
        sid: str,
        role: str,
        content: MessageContent,
        run_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        self.append_session_event(
            sid,
            event_type="input.admitted" if role == "user" else "llm.message",
            turn_id=run_id or "",
            payload={
                "role": role,
                "content": content,
                "message_id": message_id or "",
            },
        )

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

    # 以 block_id 幂等追加模型消息块，并把去重检查纳入同一账本锁
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
        with self._ledger_lock(sid):
            known_ids = self._block_ids(sid)
            if block_id in known_ids:
                return False
            self.append_session_event(
                sid,
                event_type="llm.message",
                turn_id=run_id,
                step_id=f"{run_id}:{step}",
                payload={
                    "role": role,
                    "block": block,
                    "message_id": message_id,
                    "block_id": block_id,
                    "block_index": block_index,
                    "block_count": block_count,
                },
            )
            known_ids.add(block_id)
            return True

    # 追加一条 schema v2 SessionEvent 并复用 transcript 的序号和 checksum 链
    def append_session_event(
        self,
        sid: str,
        *,
        event_type: str,
        turn_id: str = "",
        step_id: str = "",
        source_event_seqs: tuple[int, ...] = (),
        payload: dict[str, Any] | None = None,
        timestamp: str | None = None,
        provenance: str = "native",
        replay_fidelity: str = "full",
    ) -> SessionEventEnvelope:
        lock = self._ledger_lock(sid)
        with lock:
            head = self._ledger_heads.get(sid)
            if head is None:
                sequence, previous, issues = self._scan_ledger(sid)
                if issues:
                    raise ValueError(
                        f"refusing to append to damaged transcript {sid}: {issues[0]}"
                    )
                self._ledger_heads[sid] = (sequence, previous)
            else:
                sequence, _previous = head
            event = SessionEventEnvelope(
                session_id=sid,
                seq=sequence + 1,
                timestamp=timestamp or _now(),
                type=event_type,
                turn_id=turn_id,
                step_id=step_id,
                source_event_seqs=source_event_seqs,
                payload=dict(payload or {}),
                provenance=provenance,
                replay_fidelity=replay_fidelity,
            )
            written_seq = self._append_ledger_row(sid, event.model_dump(mode="json"))
            if written_seq != event.seq:
                raise RuntimeError("session event seq changed during append")
            return event

    # 读取并严格解析当前会话的全部 schema v2 SessionEvent
    def read_session_events(self, sid: str) -> list[SessionEventEnvelope]:
        events: list[SessionEventEnvelope] = []
        for line_no, row in self._read_rows(sid):
            if row.get("kind") != "event":
                continue
            raw = {
                key: value
                for key, value in row.items()
                if key
                not in {"ledger_seq", "ledger_prev_checksum", "ledger_checksum"}
            }
            try:
                event = SessionEventEnvelope.model_validate(raw)
            except ValueError:
                logger.warning(
                    "skip invalid session event sid=%s line=%s",
                    sid,
                    line_no,
                    exc_info=True,
                )
                continue
            if event.seq != row.get("ledger_seq"):
                logger.warning(
                    "skip event with mismatched seq sid=%s line=%s", sid, line_no
                )
                continue
            events.append(event)
        return events

    # 从原生 v2 事件和只读 legacy 前缀投影唯一的模型消息历史
    def derive_messages(self, sid: str) -> list[dict[str, Any]]:
        rows = self._read_rows(sid)
        shadowed, replacements = self._compaction_projection(rows)
        referenced = {
            int(value)
            for _line_no, row in rows
            if row.get("kind") == "event"
            for value in row.get("source_event_seqs", [])
            if isinstance(value, int)
        }
        messages: list[dict[str, Any]] = []
        last_message_id: str | None = None
        seen_block_ids: set[str] = set()

        # 把普通消息或消息块按 message_id 稳定合并进模型历史
        def append_content(
            *,
            role: object,
            content: object | None = None,
            block: object | None = None,
            message_id: object = "",
            block_id: object = "",
        ) -> None:
            nonlocal last_message_id
            if role not in {"user", "assistant"}:
                return
            resolved_message_id = str(message_id or "")
            if isinstance(block, dict):
                resolved_block_id = str(block_id or "")
                if resolved_block_id and resolved_block_id in seen_block_ids:
                    return
                if resolved_block_id:
                    seen_block_ids.add(resolved_block_id)
                if (
                    messages
                    and last_message_id == resolved_message_id
                    and messages[-1]["role"] == role
                    and isinstance(messages[-1]["content"], list)
                ):
                    messages[-1]["content"].append(block)
                else:
                    messages.append({"role": role, "content": [block]})
                last_message_id = resolved_message_id or None
                return
            messages.append({"role": role, "content": content if content is not None else ""})
            last_message_id = resolved_message_id or None

        for _line_no, row in rows:
            ledger_seq = row.get("ledger_seq")
            if row.get("kind") == "event":
                event_type = row.get("type")
                if not isinstance(ledger_seq, int) or ledger_seq in shadowed:
                    continue
                if event_type == "context.compaction.message":
                    if ledger_seq not in replacements:
                        continue
                elif event_type not in {"input.admitted", "llm.message"}:
                    continue
                payload = row.get("payload")
                if not isinstance(payload, dict):
                    continue
                append_content(
                    role=payload.get("role"),
                    content=payload.get("content"),
                    block=payload.get("block"),
                    message_id=payload.get("message_id", ""),
                    block_id=payload.get("block_id", f"event:{ledger_seq}"),
                )
                continue
            if isinstance(ledger_seq, int) and (
                ledger_seq in referenced or ledger_seq in shadowed
            ):
                continue
            if row.get("kind") == "block":
                append_content(
                    role=row.get("role"),
                    block=row.get("block"),
                    message_id=row.get("message_id", ""),
                    block_id=row.get("block_id", ""),
                )
            else:
                append_content(
                    role=row.get("role"),
                    content=row.get("content"),
                    message_id=row.get("message_id", ""),
                )
        messages = self._trim_orphan_tool_use(messages)
        from code_rook.core.compact.budget import truncate_tool_results

        return truncate_tool_results(messages)

    # 从已提交压缩事件计算被遮蔽事实和生效替代消息，忽略未完成压缩批次
    def _compaction_projection(
        self,
        rows: list[tuple[int, dict[str, Any]]],
    ) -> tuple[set[int], set[int]]:
        shadowed: set[int] = set()
        replacements: set[int] = set()
        for _line_no, row in rows:
            if row.get("kind") != "event" or row.get("type") != "context.compaction.committed":
                continue
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            shadowed.update(
                value
                for value in payload.get("shadowed_event_seqs", [])
                if isinstance(value, int) and value > 0
            )
            replacements.update(
                value
                for value in payload.get("replacement_event_seqs", [])
                if isinstance(value, int) and value > 0
            )
        replacements.difference_update(shadowed)
        return shadowed, replacements

    # 返回当前模型投影实际消费的账本序号，供下一次压缩建立 shadow 范围
    def _active_model_ledger_seqs(self, sid: str) -> list[int]:
        rows = self._read_rows(sid)
        shadowed, replacements = self._compaction_projection(rows)
        referenced = {
            int(value)
            for _line_no, row in rows
            if row.get("kind") == "event"
            for value in row.get("source_event_seqs", [])
            if isinstance(value, int)
        }
        active: list[int] = []
        for _line_no, row in rows:
            sequence = row.get("ledger_seq")
            if not isinstance(sequence, int) or sequence in shadowed:
                continue
            if row.get("kind") == "event":
                event_type = row.get("type")
                if event_type in {"input.admitted", "llm.message"} or (
                    event_type == "context.compaction.message" and sequence in replacements
                ):
                    active.append(sequence)
            elif sequence not in referenced:
                active.append(sequence)
        return active

    # 读取完整 thread 并返回可直接传给 Anthropic 的 messages
    def read_messages(self, sid: str) -> list[dict[str, Any]]:
        return self.derive_messages(sid)

    def find_incomplete_tool_calls(self, sid: str) -> list[IncompleteToolCall]:
        _, pending, _, _ = self._scan_recovery_state(sid)
        return list(pending.values())

    # 检查账本是否存在校验链、JSON 或消息分组损坏，未配对工具本身不算文件损坏
    def has_damaged_ledger(self, sid: str) -> bool:
        _, _, damaged, _ = self._scan_recovery_state(sid)
        return damaged

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
        self._ledger_heads.pop(sid, None)

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

    # 扫描 transcript，按 run_id 收集最早与最晚时间戳（ISO 字符串可直接字典序比较）
    def run_time_ranges(self, sid: str) -> dict[str, tuple[str, str]]:
        ranges: dict[str, tuple[str, str]] = {}
        for _, row in self._read_rows(sid):
            run_id = (
                row.get("turn_id")
                if row.get("kind") == "event"
                else row.get("run_id")
            )
            ts = (
                row.get("timestamp")
                if row.get("kind") == "event"
                else row.get("ts")
            )
            if not isinstance(run_id, str) or not isinstance(ts, str) or not ts:
                continue
            first, last = ranges.get(run_id, (ts, ts))
            ranges[run_id] = (min(first, ts), max(last, ts))
        return ranges

    def _block_ids(self, sid: str) -> set[str]:
        cached = self._known_block_ids.get(sid)
        if cached is not None:
            return cached
        block_ids: set[str] = set()
        for _, row in self._read_rows(sid):
            if row.get("kind") == "block" and row.get("block_id"):
                block_ids.add(str(row["block_id"]))
                continue
            payload = row.get("payload")
            if (
                row.get("kind") == "event"
                and row.get("type") == "llm.message"
                and isinstance(payload, dict)
                and payload.get("block_id")
            ):
                block_ids.add(str(payload["block_id"]))
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
        ledger_sequence = 0
        ledger_previous = ""
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
            ledger_sequence += 1
            expected_checksum = self._ledger_checksum(ledger_previous, row)
            raw_sequence = row.get("ledger_seq")
            raw_previous = row.get("ledger_prev_checksum")
            raw_checksum = row.get("ledger_checksum")
            if (
                (raw_sequence is not None and raw_sequence != ledger_sequence)
                or (raw_previous is not None and raw_previous != ledger_previous)
                or (raw_checksum is not None and raw_checksum != expected_checksum)
            ):
                damaged = True
                continue
            ledger_previous = (
                str(raw_checksum)
                if isinstance(raw_checksum, str)
                else expected_checksum
            )

            message_row = row
            if row.get("kind") == "event":
                payload = row.get("payload")
                if (
                    row.get("source_event_seqs")
                    or row.get("type") not in {"input.admitted", "llm.message"}
                    or not isinstance(payload, dict)
                ):
                    message_row = {}
                else:
                    message_row = dict(payload)
                    message_row["run_id"] = str(row.get("turn_id", ""))
                    raw_step_id = str(row.get("step_id", ""))
                    raw_step = raw_step_id.rsplit(":", 1)[-1]
                    message_row["step"] = int(raw_step) if raw_step.isdigit() else 0

            blocks: list[dict[str, Any]] = []
            if "block" in message_row:
                block_id = str(message_row.get("block_id", ""))
                message_id = str(message_row.get("message_id", ""))
                block = message_row.get("block")
                block_index = message_row.get("block_index")
                block_count = message_row.get("block_count")
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
                message_run_ids.setdefault(
                    message_id,
                    str(message_row.get("run_id", "")),
                )
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
                content = message_row.get("content")
                if isinstance(content, list):
                    blocks.extend(block for block in content if isinstance(block, dict))

            role = message_row.get("role")
            for block in blocks:
                if role == "assistant" and block.get("type") == "tool_use":
                    tool_use_id = str(block.get("id", ""))
                    if not tool_use_id:
                        damaged = True
                        continue
                    raw_step = message_row.get("step", 0)
                    step = raw_step if isinstance(raw_step, int) else 0
                    pending[tool_use_id] = IncompleteToolCall(
                        run_id=str(message_row.get("run_id", "")),
                        tool_use_id=tool_use_id,
                        tool_name=str(block.get("name", "")),
                        step=step,
                    )
                    message_id = str(message_row.get("message_id", ""))
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

    # 返回去掉 hash 链字段后的稳定 JSON 字节
    @staticmethod
    def _ledger_payload(row: dict[str, Any]) -> bytes:
        payload = {
            key: value
            for key, value in row.items()
            if key not in {"ledger_seq", "ledger_prev_checksum", "ledger_checksum"}
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    # 依据前序 checksum 计算当前 transcript 行的链式 SHA-256
    @classmethod
    def _ledger_checksum(cls, previous: str, row: dict[str, Any]) -> str:
        digest = hashlib.sha256()
        digest.update(previous.encode("ascii"))
        digest.update(b"\n")
        digest.update(cls._ledger_payload(row))
        return digest.hexdigest()

    # 扫描 transcript hash 链并返回最后序号、checksum 与问题列表
    def _scan_ledger(self, sid: str) -> tuple[int, str, list[str]]:
        path = self.session_dir(sid) / "thread.jsonl"
        if not path.is_file():
            return 0, "", []
        sequence = 0
        previous = ""
        issues: list[str] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                issues.append(f"line {line_no}: invalid JSON")
                continue
            if not isinstance(row, dict):
                issues.append(f"line {line_no}: row is not an object")
                continue
            sequence += 1
            expected = self._ledger_checksum(previous, row)
            raw_sequence = row.get("ledger_seq")
            raw_previous = row.get("ledger_prev_checksum")
            raw_checksum = row.get("ledger_checksum")
            if raw_sequence is not None and raw_sequence != sequence:
                issues.append(
                    f"line {line_no}: ledger_seq={raw_sequence}, expected {sequence}"
                )
            if raw_previous is not None and raw_previous != previous:
                issues.append(f"line {line_no}: previous checksum mismatch")
            if raw_checksum is not None and raw_checksum != expected:
                issues.append(f"line {line_no}: checksum mismatch")
            previous = str(raw_checksum) if isinstance(raw_checksum, str) else expected
        return sequence, previous, issues

    # 验证指定 session transcript 的单调序号和 hash 链
    def verify_ledger(self, sid: str) -> list[str]:
        _sequence, _checksum, issues = self._scan_ledger(sid)
        return issues

    # 检查执行事件关系和每个请求快照摘要，供 Runtime Doctor 报告不可伪造的语义损坏
    def verify_execution_ledger(self, sid: str) -> list[str]:
        events = self.read_session_events(sid)
        issues: list[str] = []
        try:
            validate_session_events(events, allow_incomplete=True)
        except InvariantViolation as exc:
            issues.append(str(exc))
        for event in events:
            if event.type != "llm.request_prepared":
                continue
            try:
                snapshot = RequestSnapshot.model_validate(event.payload)
            except ValueError as exc:
                issues.append(f"request snapshot schema invalid at seq {event.seq}: {exc}")
                continue
            if snapshot.calculated_digest() != snapshot.digest:
                issues.append(f"request snapshot digest mismatch at seq {event.seq}")
        return issues

    # 给 transcript 行补齐序号与 hash 链后耐久追加
    def _append_ledger_row(self, sid: str, row: dict[str, Any]) -> int:
        lock = self._ledger_lock(sid)
        with lock:
            head = self._ledger_heads.get(sid)
            if head is None:
                sequence, previous, issues = self._scan_ledger(sid)
                if issues:
                    raise ValueError(
                        f"refusing to append to damaged transcript {sid}: {issues[0]}"
                    )
            else:
                sequence, previous = head
            encoded = dict(row)
            encoded["ledger_seq"] = sequence + 1
            encoded["ledger_prev_checksum"] = previous
            encoded["ledger_checksum"] = self._ledger_checksum(previous, encoded)
            self._append_jsonl(self.session_dir(sid) / "thread.jsonl", encoded)
            self._ledger_heads[sid] = (sequence + 1, str(encoded["ledger_checksum"]))
            return sequence + 1

    # 返回指定 Session 的可重入写锁并安全初始化锁表
    def _ledger_lock(self, sid: str) -> threading.RLock:
        with self._ledger_locks_guard:
            return self._ledger_locks.setdefault(sid, threading.RLock())

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

    # 追加完整压缩事务并以最后一条 committed 事件原子启用 shadow 投影
    def append_compaction(
        self,
        sid: str,
        messages: list[dict[str, Any]],
        *,
        run_id: str,
        summary: str = "",
        trigger: str = "auto",
        original_tokens: int = 0,
        compacted_tokens: int = 0,
        pinned_fact_count: int = 0,
    ) -> tuple[list[int], list[int]]:
        shadowed = self._active_model_ledger_seqs(sid)
        if not shadowed:
            return [], []
        started = self.append_session_event(
            sid,
            event_type="context.compaction.started",
            turn_id=run_id,
            source_event_seqs=tuple(shadowed),
            payload={
                "shadow_start_seq": min(shadowed),
                "shadow_end_seq": max(shadowed),
                "trigger": trigger,
            },
            provenance="compaction",
        )
        replacement_seqs: list[int] = []
        for index, message in enumerate(messages, 1):
            replacement = self.append_session_event(
                sid,
                event_type="context.compaction.message",
                turn_id=run_id,
                source_event_seqs=tuple(shadowed),
                payload={
                    "role": message.get("role", "user"),
                    "content": message.get("content", ""),
                    "message_id": f"compaction:{started.seq}:{index}",
                },
                provenance="compaction",
                replay_fidelity="compacted",
            )
            replacement_seqs.append(replacement.seq)
        self.append_session_event(
            sid,
            event_type="context.compaction.summary",
            turn_id=run_id,
            source_event_seqs=tuple(shadowed),
            payload={"summary": summary, "pinned_fact_count": pinned_fact_count},
            provenance="compaction",
        )
        self.append_session_event(
            sid,
            event_type="context.compaction.committed",
            turn_id=run_id,
            source_event_seqs=tuple((*shadowed, *replacement_seqs)),
            payload={
                "shadowed_event_seqs": shadowed,
                "replacement_event_seqs": replacement_seqs,
                "original_tokens": original_tokens,
                "compacted_tokens": compacted_tokens,
                "started_seq": started.seq,
            },
            provenance="compaction",
            replay_fidelity="compacted",
        )
        return shadowed, replacement_seqs

    # 兼容旧调用方并改为追加式压缩，永不重写 thread.jsonl
    def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        self.append_compaction(
            sid,
            messages,
            run_id="legacy-compaction",
            summary="legacy compaction projection",
            trigger="legacy_api",
        )

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
    # 绑定会话账本与当前 run，供循环记录模型可见内容和请求快照
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

    # 将即将发送给 Provider 的不可变请求快照写入事实日志
    def append_request_snapshot(self, step: int, snapshot: RequestSnapshot) -> int:
        event = self._store.append_session_event(
            self._session_id,
            event_type="llm.request_prepared",
            turn_id=self._run_id,
            step_id=f"{self._run_id}:{step}",
            payload=snapshot.model_dump(mode="json"),
        )
        return event.seq

    # 读取并验证本 run 最近一次已持久化的请求快照
    def latest_request_snapshot(self) -> RequestSnapshot | None:
        for event in reversed(self._store.read_session_events(self._session_id)):
            if event.type == "llm.request_prepared" and event.turn_id == self._run_id:
                return RequestSnapshot.model_validate(event.payload)
        return None

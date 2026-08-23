from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore, SessionTranscriptSink


# 功能：验证 SessionStore 初始化时自动创建 sessions 根目录
# 设计：传入 tmp_path 下不存在的目录，断言目录被创建，覆盖首次启动 daemon 的冷路径
def test_store_creates_root(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    SessionStore(root)
    assert root.exists()


# 功能：验证 session meta 写入后能完整读回
# 设计：构造含 run_ids 的 Session，经过 JSON 文件往返后断言字段保持，覆盖 meta.json 的持久化契约
def test_meta_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = Session(
        id="sess-1",
        mode="chat",
        status="waiting_for_input",
        title="hello",
        created_at="t1",
        updated_at="t2",
        run_ids=["run-1"],
        workspace=str(tmp_path),
    )
    store.write_meta(session)
    loaded = store.read_meta("sess-1")
    assert loaded == session


# 功能：验证未来 session meta schema 会明确阻断且不会被旧版 SessionStore 改写
# 设计：手写 version=99 的完整 meta 后只调用读取，断言错误并逐字比较文件仍保持原样
def test_future_session_schema_blocks_unsupported_downgrade(tmp_path: Path) -> None:
    session_dir = tmp_path / "sess-future"
    session_dir.mkdir()
    payload = {
        "schema_version": 99,
        "id": "sess-future",
        "mode": "chat",
        "status": "active",
        "title": "future",
        "created_at": "t1",
        "updated_at": "t2",
        "run_ids": [],
        "parent_session_id": None,
    }
    original = json.dumps(payload, ensure_ascii=False) + "\n"
    meta = session_dir / "meta.json"
    meta.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="newer than supported"):
        SessionStore(tmp_path).read_meta("sess-future")

    assert meta.read_text(encoding="utf-8") == original


# 功能：验证含 tool_use/tool_result block 的 thread 消息能按 Anthropic 格式读回
# 设计：追加 assistant tool_use 和 user tool_result，读取时应剥离 ts/run_id，只保留 API messages 所需字段
def test_thread_message_roundtrip_with_tool_blocks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "read file")
    store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}],
        run_id="run-1",
    )
    store.append_message(
        "sess-1",
        "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        run_id="run-1",
    )

    messages = store.read_messages("sess-1")
    assert messages == [
        {"role": "user", "content": "read file"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]


# 功能：验证 thread 尾部孤儿 tool_use 会被裁掉
# 设计：构造一条未配对 tool_result 的 assistant tool_use，读取时只返回最后一次配平之前的消息，避免 API 报 messages.invalid
def test_read_messages_trims_orphan_tool_use_tail(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "hello")
    store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "orphan", "name": "read_file", "input": {}}],
        run_id="run-1",
    )
    assert store.read_messages("sess-1") == [{"role": "user", "content": "hello"}]


# 功能：验证 notes.md 不存在时读为空，追加笔记后能读到内容和 run_id
# 设计：先读空状态再追加，覆盖 chat 第一轮前和 note_save 调用后的两个关键状态
def test_notes_read_and_append(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store.read_notes("sess-1") == ""
    store.append_note("sess-1", "Python 3.12", "run-1")
    notes = store.read_notes("sess-1")
    assert "Python 3.12" in notes
    assert "run-1" in notes


# 功能：扫描 session 元数据时按更新时间倒序返回，并跳过损坏文件
# 设计：混合两个有效 session 和一个非法 JSON，验证 daemon 冷启动可容错恢复
def test_list_sessions_sorted_and_skips_corrupt_metadata(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    older = Session("sess-old", "chat", "closed", "old", "t1", "2026-01-01", [])
    newer = Session("sess-new", "chat", "waiting_for_input", "new", "t1", "2026-02-01", [])
    store.write_meta(older)
    store.write_meta(newer)
    corrupt_dir = tmp_path / "sess-corrupt"
    corrupt_dir.mkdir()
    (corrupt_dir / "meta.json").write_text("{broken", encoding="utf-8")

    assert [session.id for session in store.list_sessions()] == ["sess-new", "sess-old"]


# 功能：拒绝把任意路径当作 session ID，避免 session RPC 目录穿越
def test_session_dir_rejects_invalid_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(ValueError):
        store.session_dir("../outside")


def test_transcript_blocks_are_durable_grouped_and_deduplicated(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message(
        "sess-1",
        "user",
        "inspect",
        run_id="run-1",
        message_id="run-1:user",
    )
    transcript = SessionTranscriptSink(store, "sess-1", "run-1")
    assistant_blocks: list[dict[str, object]] = [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": "tool-1", "name": "read_file", "input": {}},
    ]

    transcript.append_assistant(1, assistant_blocks)
    transcript.append_assistant(1, assistant_blocks)
    transcript.append_tool_result(
        1,
        "tool-1",
        "contents",
        is_error=False,
        block_index=0,
        block_count=1,
    )

    raw_rows = [
        json.loads(line)
        for line in (store.session_dir("sess-1") / "thread.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [row["kind"] for row in raw_rows] == ["message", "block", "block", "block"]
    assert [row["block_index"] for row in raw_rows[1:3]] == [0, 1]
    assert all(row["block_count"] == 2 for row in raw_rows[1:3])
    assert store.read_messages("sess-1") == [
        {"role": "user", "content": "inspect"},
        {"role": "assistant", "content": assistant_blocks},
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tool-1", "content": "contents"}
            ],
        },
    ]


def test_recover_incomplete_tool_tail_archives_and_trims_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "work", run_id="run-1")
    transcript = SessionTranscriptSink(store, "sess-1", "run-1")
    transcript.append_assistant(
        1,
        [
            {"type": "text", "text": "working"},
            {"type": "tool_use", "id": "tool-1", "name": "first", "input": {}},
            {"type": "tool_use", "id": "tool-2", "name": "second", "input": {}},
        ],
    )
    transcript.append_tool_result(
        1,
        "tool-1",
        "done",
        is_error=False,
        block_index=0,
        block_count=2,
    )

    incomplete = store.find_incomplete_tool_calls("sess-1")
    assert [call.tool_use_id for call in incomplete] == ["tool-2"]

    recovery = store.recover_incomplete_tail("sess-1")

    assert recovery is not None
    assert recovery.run_ids == ("run-1",)
    assert recovery.tool_use_ids == ("tool-2",)
    assert recovery.kept_rows == 1
    assert recovery.discarded_rows == 4
    assert recovery.archive_path.exists()
    assert len(recovery.archive_path.read_text(encoding="utf-8").splitlines()) == 5
    assert store.read_messages("sess-1") == [{"role": "user", "content": "work"}]
    assert store.recover_incomplete_tail("sess-1") is None
    journal = (store.session_dir("sess-1") / "transcript_recoveries.jsonl").read_text(
        encoding="utf-8"
    )
    assert "trim_to_last_balanced_message" in journal


def test_recover_partial_block_group_without_tool_call(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store.append_message("sess-1", "user", "hello")
    store.append_block(
        "sess-1",
        role="assistant",
        block={"type": "text", "text": "partial"},
        run_id="run-1",
        step=1,
        message_id="run-1:assistant:1",
        block_id="run-1:assistant:1:0",
        block_index=0,
        block_count=2,
    )

    recovery = store.recover_incomplete_tail("sess-1")

    assert recovery is not None
    assert recovery.run_ids == ("run-1",)
    assert recovery.tool_use_ids == ()
    assert store.read_messages("sess-1") == [{"role": "user", "content": "hello"}]


def test_fork_copies_current_context_but_not_run_artifacts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    source = Session(
        "sess-source",
        "chat",
        "waiting_for_input",
        "source",
        "2026-01-01",
        "2026-01-02",
        ["run-old"],
    )
    forked = Session(
        "sess-forked",
        "chat",
        "waiting_for_input",
        "forked",
        "2026-01-03",
        "2026-01-03",
        [],
        parent_session_id=source.id,
    )
    store.write_meta(source)
    store.append_message(source.id, "user", "hello", run_id="run-old")
    store.append_note(source.id, "remember this", "run-old")
    runs = store.runs_dir(source.id)
    runs.mkdir(parents=True)
    (runs / "artifact.txt").write_text("source only", encoding="utf-8")
    (store.session_dir(source.id) / "thread_old.jsonl.bak").write_text(
        "archive",
        encoding="utf-8",
    )

    store.create_fork(source.id, forked)

    assert store.read_meta(forked.id).parent_session_id == source.id
    assert store.read_messages(forked.id) == [{"role": "user", "content": "hello"}]
    assert "remember this" in store.read_notes(forked.id)
    assert not store.runs_dir(forked.id).exists()
    assert not (store.session_dir(forked.id) / "thread_old.jsonl.bak").exists()


def test_delete_session_removes_directory_and_cached_blocks(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = Session("sess-delete", "chat", "closed", "", "t1", "t2", [])
    store.write_meta(session)
    store.append_block(
        session.id,
        role="assistant",
        block={"type": "text", "text": "cached"},
        run_id="run-1",
        step=1,
        message_id="message-1",
        block_id="block-1",
        block_index=0,
        block_count=1,
    )

    store.delete_session(session.id)

    assert not store.session_dir(session.id).exists()
    assert session.id not in store._known_block_ids  # type: ignore[attr-defined]
    assert not list(tmp_path.glob(".deleted-sess-*"))


# 功能：验证 transcript 行携带单调序号和链式 checksum，内容篡改会被识别并裁掉
# 设计：先写三条合法消息再改动中间行正文，断言 verify 报错且恢复后 hash 链重新健康
def test_transcript_checksum_chain_detects_and_recovers_tampering(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = "sess-checksum"
    store.append_message(sid, "user", "first", run_id="run-1")
    store.append_message(sid, "assistant", "second", run_id="run-1")
    store.append_message(sid, "user", "third", run_id="run-2")
    path = store.session_dir(sid) / "thread.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[1] = lines[1].replace("second", "tampered")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert any("checksum mismatch" in issue for issue in store.verify_ledger(sid))
    recovery = store.recover_incomplete_tail(sid)

    assert recovery is not None
    assert recovery.kept_rows == 1
    assert store.verify_ledger(sid) == []
    assert store.read_messages(sid) == [{"role": "user", "content": "first"}]


# 功能：验证 100 个 transcript 截断点都能恢复为健康前缀而不保留伪成功尾部
# 设计：对同一带 hash 链账本按均匀字节位置制造独立故障，统计恢复后可验证比例固定为 100%
def test_transcript_crash_injection_matrix_recovers_100_prefixes(tmp_path: Path) -> None:
    source = SessionStore(tmp_path / "source")
    sid = "sess-source"
    for index in range(12):
        source.append_message(
            sid,
            "user" if index % 2 == 0 else "assistant",
            f"message-{index:02d}-" + "x" * 40,
            run_id=f"run-{index // 2}",
        )
    raw = (source.session_dir(sid) / "thread.jsonl").read_bytes()
    healthy = 0
    for case in range(1, 101):
        store = SessionStore(tmp_path / f"case-{case}")
        case_sid = f"sess-case-{case}"
        path = store.session_dir(case_sid) / "thread.jsonl"
        path.parent.mkdir(parents=True)
        cutoff = max(1, len(raw) * case // 100)
        path.write_bytes(raw[:cutoff])
        store.recover_incomplete_tail(case_sid)
        if store.verify_ledger(case_sid) == []:
            healthy += 1

    assert healthy == 100

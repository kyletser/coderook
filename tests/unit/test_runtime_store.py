import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from code_rook.core.authority import AuthorityProfile, ToolAction, WorkspaceTrust
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.runtime.migrations import (
    CURRENT_SCHEMA_VERSION,
    RuntimeMigrationError,
    _apply_v1,
    _apply_v2,
    _apply_v3,
    _apply_v4,
)
from code_rook.core.runtime.models import (
    QueuedMessageRecord,
    ThreadRecord,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)
from code_rook.core.runtime.store import (
    DuplicateTerminalResultError,
    IncompleteToolCallError,
    InvalidTurnTransitionError,
    QueuedMessageDispatchingError,
    RuntimeStore,
    ToolCallNotFoundError,
)


# 生成稳定的 UTC 测试时间
def _now() -> datetime:
    return datetime(2026, 7, 30, 0, 0, tzinfo=UTC)


# 创建带 thread 和 running turn 的隔离 runtime store
def _store_with_turn(tmp_path: Path) -> RuntimeStore:
    store = RuntimeStore(tmp_path / "runtime.db")
    now = _now()
    store.create_thread(
        ThreadRecord(
            id="thread-1",
            title="Runtime test",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    store.create_turn(
        TurnRecord(
            id="turn-1",
            thread_id="thread-1",
            status=TurnStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
    )
    return store


# 功能：验证 migration 可重复执行且保留当前 schema 版本
# 设计：对同一路径连续构造两个 store，覆盖首次建库和幂等重开两条路径
def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"

    first = RuntimeStore(path)
    second = RuntimeStore(path)

    assert first.schema_version() == CURRENT_SCHEMA_VERSION
    assert second.schema_version() == CURRENT_SCHEMA_VERSION


# 功能：验证消息队列跨数据库重开、领取、中断恢复、重试与删除保持同一记录
# 设计：用真实 SQLite 顺序走完整状态机，证明 Web/TUI 不依赖各自进程内存保存后续消息
def test_durable_message_queue_survives_restart_and_requires_retry(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    store = RuntimeStore(path)
    now = _now()
    store.create_thread(
        ThreadRecord(
            id="thread-queue",
            title="Queue",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    record = QueuedMessageRecord(
        id="queue-1",
        thread_id="thread-queue",
        content="internal prompt",
        display_content="继续修复",
        created_at=now,
        updated_at=now,
    )

    store.enqueue_message(record)
    reopened = RuntimeStore(path)
    assert reopened.list_queued_messages("thread-queue") == [record]

    claimed = reopened.claim_next_queued_message("thread-queue", now)
    assert claimed is not None
    assert claimed.status == "dispatching"
    with pytest.raises(QueuedMessageDispatchingError, match="already dispatching"):
        reopened.delete_queued_message("thread-queue", "queue-1")
    reopened.defer_queued_message("thread-queue", "queue-1", now)
    assert reopened.list_queued_messages("thread-queue")[0].status == "queued"
    claimed = reopened.claim_next_queued_message("thread-queue", now)
    assert claimed is not None
    assert reopened.recover_queued_messages(now) == 1
    blocked = reopened.list_queued_messages("thread-queue")[0]
    assert blocked.status == "blocked"
    assert "daemon restarted" in blocked.error

    reopened.retry_queued_message("thread-queue", "queue-1", now)
    assert reopened.list_queued_messages("thread-queue")[0].status == "queued"
    reopened.delete_queued_message("thread-queue", "queue-1")
    assert reopened.list_queued_messages("thread-queue") == []


# 功能：验证长会话 Turn 分页返回游标前紧邻记录且维持正序
# 设计：写入五个稳定时间与 ID 的 Turn，分别读取最新页和上一页，覆盖倒序查询再正序展示的边界
def test_turn_pagination_returns_recent_pages_in_chronological_order(
    tmp_path: Path,
) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    now = _now()
    store.create_thread(
        ThreadRecord(
            id="thread-pages",
            title="Pages",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    for index in range(1, 6):
        created = now + timedelta(minutes=index)
        store.create_turn(
            TurnRecord(
                id=f"turn-{index}",
                thread_id="thread-pages",
                status=TurnStatus.RUNNING,
                created_at=created,
                updated_at=created,
            )
        )

    latest = store.list_turns("thread-pages", limit=3)
    older = store.list_turns(
        "thread-pages",
        limit=2,
        before_turn_id="turn-3",
    )

    assert [turn.id for turn in latest] == ["turn-3", "turn-4", "turn-5"]
    assert [turn.id for turn in older] == ["turn-1", "turn-2"]


# 功能：验证较新 runtime schema 不会被旧版 daemon 静默降级或覆盖
# 设计：只写入未来 user_version 后打开 RuntimeStore，断言在任何表变更前明确阻断
def test_future_runtime_schema_blocks_unsupported_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "runtime-future.db"
    future_version = CURRENT_SCHEMA_VERSION + 100
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {future_version}")
    connection.close()

    with pytest.raises(RuntimeMigrationError, match="newer than supported"):
        RuntimeStore(path)


# 功能：验证普通 store 操作结束后不会遗留阻止文件移动的 SQLite 句柄
# 设计：在 Windows 上重命名数据库文件，直接覆盖连接未关闭时最常见的失败表现
def test_store_operations_release_database_handles(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    moved = tmp_path / "runtime-moved.db"
    store = RuntimeStore(path)
    assert store.schema_version() == CURRENT_SCHEMA_VERSION

    path.rename(moved)

    assert moved.is_file()


# 功能：验证 schema v2 数据库升级后旧 turn 获得保守且可读取的 authority 默认值
# 设计：直接构造 v2 表和记录再打开 RuntimeStore，覆盖真实 ALTER migration 而非仅测试新库
def test_v2_migration_adds_frozen_authority_defaults(tmp_path: Path) -> None:
    path = tmp_path / "runtime-v2.db"
    now = _now().isoformat()
    connection = sqlite3.connect(path)
    _apply_v1(connection)
    _apply_v2(connection)
    connection.execute("PRAGMA user_version = 2")
    connection.execute(
        """
        INSERT INTO runtime_threads (
            id, title, workspace, status, default_route_id,
            created_at, updated_at, schema_version
        ) VALUES ('thread-v2', 'legacy', ?, 'idle', NULL, ?, ?, 1)
        """,
        (str(tmp_path), now, now),
    )
    connection.execute(
        "INSERT INTO runtime_event_counters (thread_id, next_seq) VALUES ('thread-v2', 1)"
    )
    connection.execute(
        """
        INSERT INTO runtime_turns (
            id, thread_id, status, mode, authority_profile,
            route_json, usage_json, error_json, boot_id,
            created_at, updated_at, schema_version
        ) VALUES (
            'turn-v2', 'thread-v2', 'completed', 'act', 'ask',
            NULL, '{}', NULL, NULL, ?, ?, 1
        )
        """,
        (now, now),
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(path)
    turn = store.get_turn("turn-v2")

    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert turn.authority_profile == AuthorityProfile.ASK
    assert turn.workspace_trust == WorkspaceTrust.UNTRUSTED
    assert turn.sandbox.available is False
    assert turn.allowed_actions == frozenset(ToolAction)


# 功能：验证 v3 迁移在首列已提交但 user_version 仍为二时可以安全重试
# 设计：人工构造崩溃后的部分 ALTER 状态，再由 RuntimeStore 补齐其余列并升级版本
def test_v3_migration_recovers_from_partially_added_columns(tmp_path: Path) -> None:
    path = tmp_path / "runtime-v3-partial.db"
    connection = sqlite3.connect(path)
    _apply_v1(connection)
    _apply_v2(connection)
    connection.execute(
        "ALTER TABLE runtime_turns "
        "ADD COLUMN workspace_trust TEXT NOT NULL DEFAULT 'untrusted'"
    )
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()

    store = RuntimeStore(path)
    inspection = sqlite3.connect(path)
    try:
        columns = {
            str(row[1])
            for row in inspection.execute("PRAGMA table_info(runtime_turns)").fetchall()
        }
    finally:
        inspection.close()

    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert {"workspace_trust", "sandbox_json", "allowed_actions_json"} <= columns


# 功能：验证 v3 facade 升级后获得当前逐行 schema 且旧记录仍可读取
# 设计：直接构造无 schema_version 列的 v3 数据库，再检查 v4 ALTER 默认值和模型往返
def test_v4_migration_adds_session_facade_record_schema(tmp_path: Path) -> None:
    path = tmp_path / "runtime-v3-facade.db"
    now = _now().isoformat()
    connection = sqlite3.connect(path)
    _apply_v1(connection)
    _apply_v2(connection)
    _apply_v3(connection)
    connection.execute("PRAGMA user_version = 3")
    connection.execute(
        """
        INSERT INTO runtime_threads (
            id, title, workspace, status, default_route_id,
            created_at, updated_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("thread-v3", "legacy facade", str(tmp_path), "idle", None, now, now, 1),
    )
    connection.execute(
        "INSERT INTO runtime_session_facades (thread_id, mode, parent_thread_id) "
        "VALUES (?, ?, ?)",
        ("thread-v3", "chat", None),
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(path)
    facade = store.get_session_facade("thread-v3")
    inspection = sqlite3.connect(path)
    columns = {
        str(row[1])
        for row in inspection.execute(
            "PRAGMA table_info(runtime_session_facades)"
        ).fetchall()
    }
    inspection.close()

    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert facade.schema_version == 1
    assert "schema_version" in columns


# 功能：验证早期 Web 候选版误写为 schema 3 的 runtime event 能在升级后继续回放
# 设计：直接构造 v4 数据库及兼容旧事件，启动 RuntimeStore 后核对版本归一化与正文无损
def test_v5_migration_repairs_legacy_runtime_event_marker(tmp_path: Path) -> None:
    path = tmp_path / "runtime-v4-events.db"
    now = _now().isoformat()
    connection = sqlite3.connect(path)
    _apply_v1(connection)
    _apply_v2(connection)
    _apply_v3(connection)
    _apply_v4(connection)
    connection.execute("PRAGMA user_version = 4")
    connection.execute(
        """
        INSERT INTO runtime_threads (
            id, title, workspace, status, default_route_id,
            created_at, updated_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("thread-v4", "legacy event", str(tmp_path), "idle", None, now, now, 1),
    )
    connection.execute(
        "INSERT INTO runtime_event_counters (thread_id, next_seq) VALUES (?, ?)",
        ("thread-v4", 2),
    )
    connection.execute(
        """
        INSERT INTO runtime_events (
            thread_id, turn_id, seq, type, payload_json, ts, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("thread-v4", None, 1, "legacy.event", '{"value":1}', now, 3),
    )
    connection.commit()
    connection.close()

    store = RuntimeStore(path)
    events = store.list_events("thread-v4")

    assert store.schema_version() == CURRENT_SCHEMA_VERSION
    assert events[0].schema_version == 1
    assert events[0].payload == {"value": 1}


# 功能：验证 thread 和 turn 可无损写入并读取
# 设计：写入包含 route、usage 和 boot_id 的记录，覆盖 JSON 与枚举字段的往返序列化
def test_thread_and_turn_roundtrip(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    now = _now()
    thread = ThreadRecord(
        id="thread-1",
        title="Roundtrip",
        workspace=str(tmp_path),
        default_route_id="route-1",
        created_at=now,
        updated_at=now,
    )
    turn = TurnRecord(
        id="turn-1",
        thread_id=thread.id,
        status=TurnStatus.RUNNING,
        route=RouteReceipt(
            route_id="route-1",
            wire_format="openai_chat",
            base_url_origin="https://api.example.test",
            model="test-model",
            credential_source="file",
        ),
        usage={"input_tokens": 12},
        boot_id="boot-1",
        created_at=now,
        updated_at=now,
    )

    store.create_thread(thread)
    store.create_turn(turn)

    assert store.get_thread(thread.id) == thread
    assert store.get_turn(turn.id) == turn


# 功能：验证 item、状态变化和 event 在同一事务中提交
# 设计：一次调用完成 message 写入和 turn 完成，再分别查询三类记录确认原子结果
def test_records_item_status_and_event_atomically(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    item = TurnItemRecord(
        id="item-1",
        turn_id="turn-1",
        kind=TurnItemKind.MESSAGE,
        payload={"role": "assistant", "content": "done"},
        created_at=now,
    )

    event = store.record_item_and_event(
        item,
        event_type="turn.completed",
        event_payload={"status": "completed"},
        event_ts=now,
        turn_status=TurnStatus.COMPLETED,
    )

    assert event.seq == 1
    assert store.list_items("turn-1") == [item]
    assert store.get_turn("turn-1").status == TurnStatus.COMPLETED
    assert store.list_events("thread-1") == [event]


# 功能：SQLite transaction 在 item 写入后中断时回滚 item、状态和事件序号
# 设计：注入 event insert 故障命中事务中段，恢复后追加事件仍获得 seq=1，证明没有半提交或序号空洞
def test_transaction_interruption_rolls_back_all_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store_with_turn(tmp_path)
    original_insert_event = store._insert_event
    item = TurnItemRecord(
        id="item-interrupted",
        turn_id="turn-1",
        kind=TurnItemKind.MESSAGE,
        payload={"content": "must roll back"},
        created_at=_now(),
    )

    # 在 item 和状态已写入当前事务后模拟存储层中断
    def interrupt_event_insert(*args: object, **kwargs: object) -> None:
        raise OSError("injected transaction interruption")

    monkeypatch.setattr(store, "_insert_event", interrupt_event_insert)
    with pytest.raises(OSError, match="transaction interruption"):
        store.record_item_and_event(
            item,
            event_type="turn.completed",
            event_payload={"status": "completed"},
            event_ts=_now(),
            turn_status=TurnStatus.COMPLETED,
        )

    assert store.list_items("turn-1") == []
    assert store.get_turn("turn-1").status == TurnStatus.RUNNING
    assert store.list_events("thread-1") == []

    monkeypatch.setattr(store, "_insert_event", original_insert_event)
    event = store.append_event(
        thread_id="thread-1",
        turn_id="turn-1",
        event_type="test.recovered",
        payload={},
        ts=_now(),
    )
    assert event.seq == 1


# 功能：验证工具结果必须引用同一 turn 中已有的工具调用
# 设计：直接写入孤立 result 并检查事务回滚，避免产生 item 或占用 event seq
def test_tool_result_requires_existing_call(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    result = TurnItemRecord(
        id="result-1",
        turn_id="turn-1",
        kind=TurnItemKind.TOOL_RESULT,
        payload={"content": "orphan"},
        tool_call_id="call-1",
        created_at=_now(),
    )

    with pytest.raises(ToolCallNotFoundError):
        store.record_item_and_event(
            result,
            event_type="tool.finished",
            event_payload={},
            event_ts=_now(),
        )

    assert store.list_items("turn-1") == []
    assert store.list_events("thread-1") == []


# 功能：验证同一工具调用只能写入一个终态结果
# 设计：先写 call 和首个 result，再尝试第二个 result，检查数据与 seq 均未增加
def test_duplicate_terminal_result_is_rejected(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    call = TurnItemRecord(
        id="call-item-1",
        turn_id="turn-1",
        kind=TurnItemKind.TOOL_CALL,
        payload={"name": "File"},
        tool_call_id="call-1",
        created_at=now,
    )
    result = TurnItemRecord(
        id="result-1",
        turn_id="turn-1",
        kind=TurnItemKind.TOOL_RESULT,
        payload={"content": "ok"},
        tool_call_id="call-1",
        created_at=now + timedelta(seconds=1),
    )
    duplicate = result.model_copy(
        update={
            "id": "result-2",
            "created_at": now + timedelta(seconds=2),
        }
    )

    store.record_item_and_event(
        call,
        event_type="tool.started",
        event_payload={},
        event_ts=now,
    )
    store.record_item_and_event(
        result,
        event_type="tool.finished",
        event_payload={},
        event_ts=now + timedelta(seconds=1),
    )

    with pytest.raises(DuplicateTerminalResultError):
        store.record_item_and_event(
            duplicate,
            event_type="tool.finished",
            event_payload={},
            event_ts=now + timedelta(seconds=2),
        )

    assert [item.id for item in store.list_items("turn-1")] == [
        "call-item-1",
        "result-1",
    ]
    assert [event.seq for event in store.list_events("thread-1")] == [1, 2]


# 功能：验证 terminal turn 不接受新 item 或反向状态变化
# 设计：先原子完成 turn，再尝试回到 running，确认第二个事务完整回滚
def test_terminal_turn_rejects_new_items(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    first = TurnItemRecord(
        id="item-1",
        turn_id="turn-1",
        kind=TurnItemKind.MESSAGE,
        payload={"content": "done"},
        created_at=now,
    )
    second = TurnItemRecord(
        id="item-2",
        turn_id="turn-1",
        kind=TurnItemKind.MESSAGE,
        payload={"content": "too late"},
        created_at=now + timedelta(seconds=1),
    )
    store.record_item_and_event(
        first,
        event_type="turn.completed",
        event_payload={},
        event_ts=now,
        turn_status=TurnStatus.COMPLETED,
    )

    with pytest.raises(InvalidTurnTransitionError):
        store.record_item_and_event(
            second,
            event_type="turn.resumed",
            event_payload={},
            event_ts=now + timedelta(seconds=1),
            turn_status=TurnStatus.RUNNING,
        )

    assert [item.id for item in store.list_items("turn-1")] == ["item-1"]
    assert [event.seq for event in store.list_events("thread-1")] == [1]


# 功能：验证 terminal turn 不能重复追加第二个终态事件
# 设计：先完成 running turn，再重复执行相同 transition，检查状态和事件序号都保持不变
def test_terminal_transition_cannot_be_repeated(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    store.transition_turn_and_event(
        "turn-1",
        status=TurnStatus.COMPLETED,
        event_type="turn.completed",
        event_payload={},
        event_ts=now,
    )

    with pytest.raises(InvalidTurnTransitionError):
        store.transition_turn_and_event(
            "turn-1",
            status=TurnStatus.COMPLETED,
            event_type="turn.completed",
            event_payload={},
            event_ts=now + timedelta(seconds=1),
        )

    assert store.get_turn("turn-1").status == TurnStatus.COMPLETED
    assert [event.seq for event in store.list_events("thread-1")] == [1]


# 功能：验证并发事件追加仍分配连续且唯一的 thread seq
# 设计：多个线程共享 store 但各自打开 SQLite 连接，直接覆盖真实写锁竞争路径
def test_concurrent_events_receive_contiguous_seq(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()

    # 并发追加一个带唯一序号提示的测试事件
    def append(index: int) -> int:
        event = store.append_event(
            thread_id="thread-1",
            turn_id="turn-1",
            event_type="test.concurrent",
            payload={"index": index},
            ts=now + timedelta(milliseconds=index),
        )
        return event.seq

    with ThreadPoolExecutor(max_workers=8) as executor:
        seqs = list(executor.map(append, range(24)))

    assert sorted(seqs) == list(range(1, 25))
    assert [event.seq for event in store.list_events("thread-1")] == list(range(1, 25))


# 功能：验证存在未配对工具调用时 turn 不能进入终态，补写结果后可原子完成
# 设计：先触发 transition 回滚，再用 result 与 completed 状态同事务提交，覆盖两条终态入口
def test_terminal_turn_requires_paired_tool_results(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    store.record_item_and_event(
        TurnItemRecord(
            id="call-item-1",
            turn_id="turn-1",
            kind=TurnItemKind.TOOL_CALL,
            payload={"name": "File"},
            tool_call_id="call-1",
            created_at=now,
        ),
        event_type="tool.started",
        event_payload={},
        event_ts=now,
    )

    with pytest.raises(IncompleteToolCallError):
        store.transition_turn_and_event(
            "turn-1",
            status=TurnStatus.COMPLETED,
            event_type="turn.completed",
            event_payload={},
            event_ts=now + timedelta(seconds=1),
        )

    assert store.get_turn("turn-1").status == TurnStatus.RUNNING
    result_event = store.record_item_and_event(
        TurnItemRecord(
            id="result-item-1",
            turn_id="turn-1",
            kind=TurnItemKind.TOOL_RESULT,
            payload={"content": "ok"},
            tool_call_id="call-1",
            created_at=now + timedelta(seconds=2),
        ),
        event_type="turn.completed",
        event_payload={"status": "completed"},
        event_ts=now + timedelta(seconds=2),
        turn_status=TurnStatus.COMPLETED,
    )

    assert result_event.seq == 2
    assert store.get_turn("turn-1").status == TurnStatus.COMPLETED


# 功能：验证 runtime 事件查询同时遵守 after_seq、up_to_seq 和 limit 游标边界
# 设计：写入五个连续事件后组合分页参数，精确断言结果序号和当前高水位
def test_event_cursor_honors_high_water_and_limit(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    for index in range(5):
        store.append_event(
            thread_id="thread-1",
            turn_id="turn-1",
            event_type="test.event",
            payload={"index": index},
            ts=now + timedelta(seconds=index),
        )

    events = store.list_events(
        "thread-1",
        after_seq=1,
        up_to_seq=4,
        limit=2,
    )

    assert [event.seq for event in events] == [2, 3]
    assert store.latest_event_seq("thread-1") == 5


# 功能：验证 daemon 重启会中断旧 boot 的活动 turn 并为孤立工具调用合成错误结果
# 设计：构造旧 boot 的 running turn，连续恢复两次，检查首次原子修复和第二次幂等空操作
def test_recover_stale_turns_repairs_tool_pair_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    store.record_item_and_event(
        TurnItemRecord(
            id="call-item-1",
            turn_id="turn-1",
            kind=TurnItemKind.TOOL_CALL,
            payload={"name": "File"},
            tool_call_id="call-1",
            created_at=now,
        ),
        event_type="tool.started",
        event_payload={},
        event_ts=now,
    )

    recovered = store.recover_stale_turns("boot-new", now + timedelta(seconds=1))
    repeated = store.recover_stale_turns("boot-new", now + timedelta(seconds=2))

    items = store.list_items("turn-1")
    assert [item.kind for item in items] == [
        TurnItemKind.TOOL_CALL,
        TurnItemKind.TOOL_RESULT,
    ]
    assert items[-1].payload == {
        "status": "error",
        "reason": "daemon_restarted",
    }
    assert store.get_turn("turn-1").status == TurnStatus.INTERRUPTED
    assert store.get_thread("thread-1").status.value == "interrupted"
    assert [event.type for event in recovered] == ["turn.interrupted"]
    assert repeated == []


# 功能：验证旧 boot 恢复会在连续事件序列下安全重建缺失 counter
# 设计：删除已有活动 Turn 的 counter 后执行恢复，断言事件序号承接且 daemon 路径不再失败
def test_recover_stale_turns_rebuilds_missing_safe_counter(tmp_path: Path) -> None:
    store = _store_with_turn(tmp_path)
    now = _now()
    store.append_event(
        thread_id="thread-1",
        turn_id="turn-1",
        event_type="turn.started",
        payload={},
        ts=now,
    )
    connection = sqlite3.connect(store.path)
    connection.execute(
        "DELETE FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-1",),
    )
    connection.commit()
    connection.close()

    recovered = store.recover_stale_turns("boot-new", now + timedelta(seconds=1))

    assert [event.seq for event in recovered] == [2]
    assert store.latest_event_seq("thread-1") == 2

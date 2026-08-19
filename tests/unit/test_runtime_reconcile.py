from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from code_rook.core.runtime.models import ThreadRecord
from code_rook.core.runtime.reconcile import RuntimeReconciler
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore


# 创建使用同一临时状态根的 reconciler 与两种底层 store
def _reconciler(
    tmp_path: Path,
) -> tuple[RuntimeReconciler, RuntimeStore, SessionStore]:
    runtime = RuntimeStore(tmp_path / "runtime.db")
    sessions = SessionStore(tmp_path / "sessions")
    reconciler = RuntimeReconciler(
        runtime,
        sessions,
        workspace=tmp_path,
        journal_path=tmp_path / "repair.jsonl",
    )
    return reconciler, runtime, sessions


# 功能：验证缺失 thread/turn 投影会被发现并通过 bootstrap 幂等修复
# 设计：只写文件账本及 run 索引，先检查错误分类，再 repair 并确认第二次检查健康
async def test_reconcile_repairs_missing_session_projection(tmp_path: Path) -> None:
    reconciler, runtime, sessions = _reconciler(tmp_path)
    session = Session(
        id="sess-reconcile",
        mode="chat",
        status="closed",
        title="repair",
        created_at="2026-08-18T00:00:00Z",
        updated_at="2026-08-18T00:01:00Z",
        run_ids=["run-reconcile"],
    )
    sessions.write_meta(session)

    before = reconciler.inspect()
    after = await reconciler.repair()

    assert [issue.code for issue in before.issues] == ["missing_thread_projection"]
    assert after.healthy is True
    assert after.repaired == ["projection_bootstrap"]
    assert runtime.get_turn("run-reconcile").thread_id == session.id
    assert (tmp_path / "repair.jsonl").is_file()


# 功能：验证错误的事件 counter 可修复且不会改写已有事件序号
# 设计：创建空 thread 后直接篡改 next_seq，repair 应只把 counter 恢复为一
async def test_reconcile_repairs_event_counter_without_renumbering(tmp_path: Path) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-counter",
            title="counter",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    connection = sqlite3.connect(runtime.path)
    connection.execute(
        "UPDATE runtime_event_counters SET next_seq = 9 WHERE thread_id = ?",
        ("thread-counter",),
    )
    connection.commit()
    connection.close()

    before = reconciler.inspect()
    after = await reconciler.repair()

    assert any(issue.code == "event_counter_mismatch" for issue in before.issues)
    assert after.repaired == ["event_counters"]
    assert all(issue.code != "event_counter_mismatch" for issue in after.issues)

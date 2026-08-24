from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from code_rook.core import quarantine as quarantine_module
from code_rook.core.config import LlmConfig
from code_rook.core.goal.models import GoalRecord
from code_rook.core.llm.credentials import save_api_key
from code_rook.core.llm.migration_receipt import (
    ProviderCatalogMigrationReceiptStore,
    build_provider_catalog_migration_receipt,
)
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.llm.routes import get_route_preset
from code_rook.core.quarantine import quarantine_invalid_file
from code_rook.core.runtime import reconcile as reconcile_module
from code_rook.core.runtime.migrations import CURRENT_SCHEMA_VERSION, _apply_v1, _apply_v2
from code_rook.core.runtime.models import (
    SessionFacadeRecord,
    ThreadRecord,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)
from code_rook.core.runtime.reconcile import RuntimeReconciler
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import (
    RuntimeStore,
    UnsupportedRuntimeRecordSchemaError,
)
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.upgrade import ensure_v1_upgrade_backup


# 创建使用同一临时状态根的 reconciler 与两种底层 store
def _reconciler(
    tmp_path: Path,
) -> tuple[RuntimeReconciler, RuntimeStore, SessionStore]:
    ensure_v1_upgrade_backup(tmp_path)
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


# 功能：验证 Doctor 会按记录声明的类型报告已隔离状态且不会伪装成可修复
# 设计：把无效 Goal 放入嵌套目录后隔离，排除依赖目录名推断类型的错误实现
def test_reconcile_reports_quarantined_state_records(tmp_path: Path) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)
    invalid = tmp_path / "goals" / "nested" / "goal-invalid.json"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("not-json", encoding="utf-8")
    quarantined = quarantine_invalid_file(
        invalid,
        category="goal",
        reason="test invalid state",
        state_root=tmp_path,
    )

    report = reconciler.inspect()

    assert quarantined is not None
    assert report.quarantined_records == {"goal": 1}
    issue = next(item for item in report.issues if item.code == "quarantined_state_records")
    assert issue.severity == "warning"
    assert issue.repairable is False


# 功能：验证 Doctor 会把损坏的 v1 迁移备份识别为一致性错误
# 设计：在构造有效基线后篡改 manifest，断言只读检查报告 invalid 且不自动重建
def test_reconcile_reports_invalid_migration_backup(tmp_path: Path) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)
    marker = tmp_path / "migrations" / "provider-catalog-v1.json"
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    backup_dir = Path(marker_payload["backup_dir"])
    (backup_dir / "manifest.json").write_text("{}", encoding="utf-8")

    report = reconciler.inspect()

    assert report.migration_status == "invalid"
    issue = next(
        item for item in report.issues if item.code == "provider_catalog_migration_backup"
    )
    assert issue.severity == "error"
    assert marker.is_file()


# 功能：验证 Runtime Doctor 分别报告前置备份和 Provider Catalog 完成收据
# 设计：保持备份始终有效并依次观察收据缺失、完整、损坏，排除把 backup valid 当迁移完成
def test_reconcile_distinguishes_backup_from_provider_catalog_receipt(
    tmp_path: Path,
) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)

    pending = reconciler.inspect()

    assert pending.migration_status == "valid"
    assert pending.backup_status == "valid"
    assert pending.provider_catalog_status == "pending"

    routes = RouteStore(tmp_path / "routes.json")
    receipt_store = ProviderCatalogMigrationReceiptStore(tmp_path)
    receipt_store.write(
        build_provider_catalog_migration_receipt(
            LlmConfig(),
            routes,
            outcome="legacy_not_configured",
        )
    )
    complete = reconciler.inspect()

    assert complete.backup_status == "valid"
    assert complete.provider_catalog_status == "complete"

    payload = json.loads(receipt_store.path.read_text(encoding="utf-8"))
    payload["receipt_digest"] = "0" * 64
    receipt_store.path.write_text(json.dumps(payload), encoding="utf-8")
    invalid = reconciler.inspect()

    assert invalid.backup_status == "valid"
    assert invalid.provider_catalog_status == "invalid"
    assert invalid.healthy is False
    assert any(
        issue.code == "provider_catalog_migration_receipt"
        for issue in invalid.issues
    )


# 功能：验证 Runtime Doctor 只公开凭据文档的 missing/ready/invalid 状态
# 设计：依次检查缺失、有效和含密钥片段的损坏原文，断言报告脱敏且诊断不改写证据
async def test_reconcile_reports_redacted_credential_store_status(tmp_path: Path) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)
    credential_path = tmp_path / "credentials.json"

    missing = reconciler.inspect()
    save_api_key("anthropic", "private-ready-secret", credential_path)
    ready = reconciler.inspect()
    corrupt = b'{"api_keys":{"private":"never-print"}'
    credential_path.write_bytes(corrupt)
    invalid = reconciler.inspect()
    after_repair = await reconciler.repair()

    assert missing.credential_store_status == "missing"
    assert ready.credential_store_status == "ready"
    assert "private-ready-secret" not in ready.model_dump_json()
    assert invalid.credential_store_status == "invalid"
    assert invalid.healthy is False
    assert any(issue.code == "credential_store_invalid" for issue in invalid.issues)
    assert "never-print" not in invalid.model_dump_json()
    assert after_repair.credential_store_status == "invalid"
    assert credential_path.read_bytes() == corrupt


# 功能：验证 Runtime Doctor 区分缺失、可用、局部降级和整文档无效的 Route Catalog
# 设计：在同一状态根逐步添加好路由、非活动坏记录和损坏顶层 JSON，断言健康级别随风险变化
def test_reconcile_reports_route_catalog_health_without_mutation(tmp_path: Path) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)
    routes = RouteStore(tmp_path / "routes.json")

    missing = reconciler.inspect()
    routes.add(get_route_preset("ollama"), activate=True)
    ready_bytes = routes.path.read_bytes()
    ready = reconciler.inspect()

    payload = json.loads(ready_bytes)
    payload["routes"].append({"id": "invalid-route"})
    routes.path.write_text(json.dumps(payload), encoding="utf-8")
    degraded_bytes = routes.path.read_bytes()
    degraded = reconciler.inspect()

    routes.path.write_text("{", encoding="utf-8")
    invalid_bytes = routes.path.read_bytes()
    invalid = reconciler.inspect()

    assert missing.route_catalog_status == "missing"
    assert ready.route_catalog_status == "ready"
    assert degraded.route_catalog_status == "degraded"
    assert degraded.healthy is True
    assert any(
        issue.code == "provider_route_catalog_degraded"
        and issue.severity == "warning"
        for issue in degraded.issues
    )
    assert routes.path.read_bytes() == invalid_bytes
    assert degraded_bytes != invalid_bytes
    assert invalid.route_catalog_status == "invalid"
    assert invalid.healthy is False
    assert any(issue.code == "provider_route_catalog_invalid" for issue in invalid.issues)


# 功能：验证只读 Doctor 保留损坏 Session，显式 repair 才隔离元数据
# 设计：检查前后文件存在性并保留同目录账本，证明检查无副作用且修复范围最小
async def test_reconcile_repairs_invalid_session_metadata_explicitly(
    tmp_path: Path,
) -> None:
    reconciler, _runtime, sessions = _reconciler(tmp_path)
    session_dir = sessions.session_dir("sess-corrupt")
    session_dir.mkdir(parents=True)
    (session_dir / "meta.json").write_text("{", encoding="utf-8")
    transcript = session_dir / "thread.jsonl"
    transcript.write_text("preserve-me\n", encoding="utf-8")

    before = reconciler.inspect()

    assert before.session_count == 0
    assert before.quarantined_records == {}
    assert any(issue.code == "invalid_session_record" for issue in before.issues)
    assert (session_dir / "meta.json").is_file()
    after = await reconciler.repair()

    assert after.quarantined_records == {"session": 1}
    assert after.repaired == ["state_quarantine"]
    assert transcript.read_text(encoding="utf-8") == "preserve-me\n"
    assert not (session_dir / "meta.json").exists()
    assert list((session_dir / "_quarantine").glob("*.invalid.json"))


# 功能：验证 Doctor 只读报告未加载的 Goal 与 Task，repair 才分类隔离
# 设计：直接写原始坏文件并检查两阶段状态，证明诊断不依赖业务加载且不会暗中写盘
async def test_reconcile_scans_then_repairs_unloaded_goal_and_task_records(
    tmp_path: Path,
) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)
    goal = tmp_path / "goals" / "goal-deadbeef0000.json"
    task = tmp_path / "sessions" / "sess-a" / "runs" / "run-a" / ".tasks" / "task_1.json"
    goal.parent.mkdir(parents=True)
    task.parent.mkdir(parents=True)
    goal.write_text("{", encoding="utf-8")
    task.write_text("[]", encoding="utf-8")

    before = reconciler.inspect()

    assert before.quarantined_records == {}
    assert {issue.code for issue in before.issues} >= {
        "invalid_goal_record",
        "invalid_task_record",
    }
    assert goal.is_file()
    assert task.is_file()
    after = await reconciler.repair()

    assert after.quarantined_records == {"goal": 1, "task": 1}
    assert sum(issue.code == "quarantined_state_records" for issue in after.issues) == 2
    assert not goal.exists()
    assert not task.exists()


# 功能：验证缺失事件 counter 会被报告并重建为最大序号加一
# 设计：删除现有 thread 的 counter 后执行 inspect/repair，覆盖 INSERT 而非仅 UPDATE 的路径
async def test_reconcile_repairs_missing_event_counter(tmp_path: Path) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-missing-counter",
            title="counter",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    connection = sqlite3.connect(runtime.path)
    connection.execute(
        "DELETE FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-missing-counter",),
    )
    connection.commit()
    connection.close()

    before = reconciler.inspect()
    after = await reconciler.repair()

    issue = next(item for item in before.issues if item.code == "missing_event_counter")
    assert issue.repairable is True
    assert after.repaired == ["event_counters"]
    connection = sqlite3.connect(runtime.path)
    row = connection.execute(
        "SELECT next_seq FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-missing-counter",),
    ).fetchone()
    connection.close()
    assert row == (1,)


# 功能：验证单条损坏 runtime thread 行只产生不健康报告而不会击穿 Doctor
# 设计：保留合法表结构并篡改枚举字段，确认严格逐行校验跳过坏行且报告原始行数
def test_reconcile_reports_invalid_runtime_thread_without_crashing(
    tmp_path: Path,
) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-corrupt",
            title="corrupt",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    connection = sqlite3.connect(runtime.path)
    connection.execute(
        "UPDATE runtime_threads SET status = 'not-a-status' WHERE id = ?",
        ("thread-corrupt",),
    )
    connection.commit()
    connection.close()

    report = reconciler.inspect()

    assert report.healthy is False
    assert report.thread_count == 1
    assert any(issue.code == "invalid_runtime_thread_record" for issue in report.issues)


# 功能：验证旧版 repair 面对未来 Runtime schema 时不会修改任何数据库内容
# 设计：同时制造未来版本和缺失 counter，按字节及 SQL 状态断言检查与修复均只读停手
async def test_reconcile_never_repairs_future_runtime_schema(tmp_path: Path) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-future",
            title="future",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    connection = sqlite3.connect(runtime.path)
    connection.execute(
        "DELETE FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-future",),
    )
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    before_bytes = runtime.path.read_bytes()

    before = reconciler.inspect()
    after = await reconciler.repair()

    assert [issue.code for issue in before.issues] == ["unsupported_runtime_schema"]
    assert after.repaired == []
    assert runtime.path.read_bytes() == before_bytes
    connection = sqlite3.connect(runtime.path)
    counter = connection.execute(
        "SELECT next_seq FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-future",),
    ).fetchone()
    version = connection.execute("PRAGMA user_version").fetchone()
    connection.close()
    assert counter is None
    assert version == (99,)


# 功能：验证隔离日志符号链接不能把诊断内容追加到状态根之外
# 设计：预置指向外部哨兵的 journal 链接，断言隔离失败关闭且源文件与外部内容均不变
def test_quarantine_refuses_symlinked_journal(tmp_path: Path) -> None:
    invalid = tmp_path / "goals" / "goal-bad.json"
    quarantine = invalid.parent / "_quarantine"
    outside = tmp_path / "outside.txt"
    quarantine.mkdir(parents=True)
    invalid.write_text("{", encoding="utf-8")
    outside.write_text("sentinel", encoding="utf-8")
    journal = quarantine / "quarantine.jsonl"
    try:
        os.symlink(outside, journal)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    result = quarantine_invalid_file(
        invalid,
        category="goal",
        reason="test unsafe journal",
        state_root=tmp_path,
    )

    assert result is None
    assert invalid.is_file()
    assert outside.read_text(encoding="utf-8") == "sentinel"


# 功能：验证 v2 Runtime 数据库只报告可升级状态并由 repair 安全迁移后再解析记录
# 设计：用旧表结构和真实 turn 行构造数据库，覆盖缺少 v3 列时不得调用当前 decoder 的路径
async def test_reconcile_upgrades_legacy_runtime_before_decoding(
    tmp_path: Path,
) -> None:
    ensure_v1_upgrade_backup(tmp_path)
    database = tmp_path / "runtime.db"
    connection = sqlite3.connect(database)
    _apply_v1(connection)
    _apply_v2(connection)
    connection.execute("PRAGMA user_version = 2")
    connection.execute(
        """
        INSERT INTO runtime_threads (
            id, title, workspace, status, default_route_id,
            created_at, updated_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "thread-v2",
            "legacy",
            str(tmp_path),
            "idle",
            None,
            "2026-08-18T00:00:00+00:00",
            "2026-08-18T00:00:00+00:00",
            1,
        ),
    )
    connection.execute(
        """
        INSERT INTO runtime_turns (
            id, thread_id, status, mode, authority_profile,
            route_json, usage_json, error_json, boot_id,
            created_at, updated_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "turn-v2",
            "thread-v2",
            "completed",
            "act",
            "ask",
            None,
            "{}",
            None,
            None,
            "2026-08-18T00:00:00+00:00",
            "2026-08-18T00:00:00+00:00",
            1,
        ),
    )
    connection.execute(
        "INSERT INTO runtime_event_counters (thread_id, next_seq) VALUES (?, ?)",
        ("thread-v2", 1),
    )
    connection.commit()
    connection.close()
    runtime = RuntimeStore(database, migrate=False)
    reconciler = RuntimeReconciler(
        runtime,
        SessionStore(tmp_path / "sessions"),
        workspace=tmp_path,
        journal_path=tmp_path / "repair.jsonl",
    )

    before = reconciler.inspect()
    after = await reconciler.repair()

    assert [issue.code for issue in before.issues] == [
        "runtime_schema_upgrade_required"
    ]
    assert after.runtime_schema_version == CURRENT_SCHEMA_VERSION
    assert after.repaired == ["runtime_schema_upgrade"]
    assert runtime.get_turn("turn-v2").thread_id == "thread-v2"


@pytest.mark.parametrize("table", ["runtime_turn_items", "runtime_session_facades"])
# 功能：验证缺失任一公开 Runtime 表都会让 Doctor 明确失败而不是假绿
# 设计：从合法 v3 数据库删除各扩展表，断言完整 schema manifest 报不可自动修复
def test_reconcile_reports_missing_runtime_tables(
    tmp_path: Path,
    table: str,
) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    connection = sqlite3.connect(runtime.path)
    connection.execute(f"DROP TABLE {table}")
    connection.commit()
    connection.close()

    report = reconciler.inspect()

    issue = next(item for item in report.issues if item.code == "runtime_schema_incomplete")
    assert report.healthy is False
    assert issue.repairable is False


# 功能：验证 Runtime 数据库 symlink 不能让 repair 修改状态根之外的 SQLite
# 设计：把缺失 counter 的外部数据库链接进状态根，比较 repair 前后字节与查询结果
async def test_reconcile_refuses_symlinked_runtime_database(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    ensure_v1_upgrade_backup(state_root)
    outside = tmp_path / "outside.db"
    external_runtime = RuntimeStore(outside)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    external_runtime.create_thread(
        ThreadRecord(
            id="thread-outside",
            title="outside",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    connection = sqlite3.connect(outside)
    connection.execute(
        "DELETE FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-outside",),
    )
    connection.commit()
    connection.close()
    link = state_root / "runtime.db"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")
    before_bytes = outside.read_bytes()
    reconciler = RuntimeReconciler(
        RuntimeStore(link, migrate=False),
        SessionStore(state_root / "sessions"),
        workspace=tmp_path,
        journal_path=state_root / "repair.jsonl",
    )

    before = reconciler.inspect()
    after = await reconciler.repair()

    assert [issue.code for issue in before.issues] == [
        "unsafe_runtime_database_path"
    ]
    assert after.repaired == []
    assert outside.read_bytes() == before_bytes
    connection = sqlite3.connect(outside)
    counter = connection.execute(
        "SELECT next_seq FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-outside",),
    ).fetchone()
    connection.close()
    assert counter is None


# 功能：验证损坏 Event、Item 与 Session Facade 行会逐条报告而不会让 Doctor 崩溃或假绿
# 设计：在合法 schema 中分别破坏 JSON 与枚举字段，保留连续 seq 以证明错误来自内容校验
def test_reconcile_validates_all_public_runtime_record_kinds(tmp_path: Path) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-records",
            title="records",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    runtime.append_event(
        thread_id="thread-records",
        turn_id=None,
        event_type="test.event",
        payload={},
        ts=now,
    )
    connection = sqlite3.connect(runtime.path)
    connection.execute(
        "UPDATE runtime_events SET payload_json = '{' WHERE thread_id = ?",
        ("thread-records",),
    )
    connection.execute(
        """
        INSERT INTO runtime_turn_items (
            id, turn_id, kind, payload_json, tool_call_id, created_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "item-corrupt",
            "turn-missing",
            "message",
            "{",
            None,
            now.isoformat(),
            1,
        ),
    )
    connection.execute(
        "INSERT INTO runtime_session_facades (thread_id, mode, parent_thread_id) "
        "VALUES (?, ?, ?)",
        ("thread-records", "invalid", None),
    )
    connection.commit()
    connection.close()

    report = reconciler.inspect()

    codes = {issue.code for issue in report.issues}
    assert report.healthy is False
    assert {
        "invalid_runtime_event_record",
        "invalid_runtime_item_record",
        "invalid_runtime_facade_record",
    }.issubset(codes)


# 功能：验证 repair 扫描后被并发修好的 Goal 不会按旧结论误移入隔离区
# 设计：在 quarantine 调用边界原子替换为同路径合法记录，依靠 no-follow 指纹前后复核拒绝移动
async def test_reconcile_does_not_quarantine_concurrently_repaired_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciler, _runtime, _sessions = _reconciler(tmp_path)
    path = tmp_path / "goals" / "goal-deadbeef0000.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    real_quarantine = reconcile_module.quarantine_invalid_file
    replacement = GoalRecord(
        id="goal-deadbeef0000",
        objective="repaired concurrently",
        created_at="2026-08-18T00:00:00Z",
        updated_at="2026-08-18T00:00:00Z",
    ).model_dump_json(indent=2) + "\n"
    replaced = False

    # 在真实隔离调用前模拟 daemon 已原子修复同一路径记录
    def replace_before_quarantine(*args, **kwargs):
        nonlocal replaced
        if not replaced:
            temporary = path.with_suffix(".replacement")
            temporary.write_text(replacement, encoding="utf-8")
            temporary.replace(path)
            replaced = True
        return real_quarantine(*args, **kwargs)

    monkeypatch.setattr(
        reconcile_module,
        "quarantine_invalid_file",
        replace_before_quarantine,
    )

    report = await reconciler.repair()

    assert replaced is True
    assert path.is_file()
    assert GoalRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))).objective == (
        "repaired concurrently"
    )
    assert not (path.parent / "_quarantine").exists()
    assert report.repaired == []


# 功能：验证隔离竞态回滚绝不会覆盖 daemon 在原路径并发写出的更新记录
# 设计：在首次移动内注入 B 并立即创建 C，断言 mismatch 后 no-clobber 恢复保留 C 与隔离证据 B
def test_quarantine_race_rollback_never_overwrites_new_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "goals" / "goal-race.json"
    source.parent.mkdir()
    source.write_text("original-a", encoding="utf-8")
    expected = quarantine_module.fingerprint_state_file(source)
    real_replace = quarantine_module.os.replace
    raced = False

    # 在隔离的原子移动边界精确模拟 daemon 先写 B、移动后又写 C
    def replace_with_race(old, new) -> None:
        nonlocal raced
        if not raced and Path(old) == source:
            replacement = source.with_suffix(".b")
            replacement.write_text("changed-before-move-b", encoding="utf-8")
            real_replace(replacement, source)
            real_replace(source, new)
            source.write_text("new-valid-concurrent-state-c", encoding="utf-8")
            raced = True
            return
        real_replace(old, new)

    monkeypatch.setattr(quarantine_module.os, "replace", replace_with_race)

    result = quarantine_module.quarantine_invalid_file(
        source,
        category="goal",
        reason="race regression",
        state_root=tmp_path,
        expected_fingerprint=expected,
    )

    assert result is None
    assert raced is True
    assert source.read_text(encoding="utf-8") == "new-valid-concurrent-state-c"
    quarantined = list((source.parent / "_quarantine").glob("*.invalid.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "changed-before-move-b"


# 功能：验证 repair journal 符号链接会在任何状态修改前失败关闭
# 设计：同时准备可修复投影和外部哨兵，断言 repair 不写外部文件也不创建 Runtime thread
async def test_reconcile_repair_refuses_symlinked_journal_before_mutation(
    tmp_path: Path,
) -> None:
    reconciler, runtime, sessions = _reconciler(tmp_path)
    sessions.write_meta(
        Session(
            id="sess-unsafe-journal",
            mode="chat",
            status="closed",
            title="unsafe journal",
            created_at="2026-08-18T00:00:00Z",
            updated_at="2026-08-18T00:01:00Z",
        )
    )
    outside = tmp_path / "outside-repair.log"
    outside.write_text("sentinel", encoding="utf-8")
    journal = tmp_path / "repair.jsonl"
    try:
        os.symlink(outside, journal)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    report = await reconciler.repair()

    assert report.healthy is False
    assert report.repaired == []
    assert any(issue.code == "unsafe_repair_journal_path" for issue in report.issues)
    assert outside.read_text(encoding="utf-8") == "sentinel"
    assert runtime.list_threads() == []


# 功能：验证 repair 状态根本身是符号链接时不会跟随到外部创建数据库或日志
# 设计：以空外部目录作为链接目标执行 repair，断言只报告边界错误且目标目录保持空白
async def test_reconcile_repair_refuses_symlinked_state_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-state"
    outside.mkdir()
    state_root = tmp_path / "linked-state"
    try:
        os.symlink(outside, state_root, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")
    reconciler = RuntimeReconciler(
        RuntimeStore(state_root / "runtime.db", migrate=False),
        SessionStore(state_root / "sessions", initialize=False),
        workspace=tmp_path,
        journal_path=state_root / "repair.jsonl",
    )

    report = await reconciler.repair()

    assert report.healthy is False
    assert report.repaired == []
    assert any(issue.code == "unsafe_repair_state_root" for issue in report.issues)
    assert list(outside.iterdir()) == []


# 功能：验证外键孤儿行让 Runtime Doctor 明确失败且 repair 不擅自删除记录
# 设计：关闭 SQLite 外键后制造 turn→thread 孤儿，覆盖逐行模型合法但关系损坏的假绿路径
async def test_reconcile_reports_foreign_key_violation_without_deleting_rows(
    tmp_path: Path,
) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-orphan-parent",
            title="parent",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    runtime.create_turn(
        TurnRecord(
            id="turn-orphan",
            thread_id="thread-orphan-parent",
            status=TurnStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
    )
    connection = sqlite3.connect(runtime.path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "DELETE FROM runtime_event_counters WHERE thread_id = ?",
        ("thread-orphan-parent",),
    )
    connection.execute(
        "DELETE FROM runtime_threads WHERE id = ?",
        ("thread-orphan-parent",),
    )
    connection.commit()
    connection.close()

    before = reconciler.inspect()
    after = await reconciler.repair()

    assert before.healthy is False
    assert any(issue.code == "runtime_foreign_key_violation" for issue in before.issues)
    assert after.healthy is False
    assert after.repaired == []
    connection = sqlite3.connect(runtime.path)
    remaining = connection.execute(
        "SELECT id FROM runtime_turns WHERE id = ?",
        ("turn-orphan",),
    ).fetchone()
    connection.close()
    assert remaining == ("turn-orphan",)


# 功能：验证五类未来 Runtime 行保持原版本并阻断 Store、Runtime API 与 repair
# 设计：把同一有效关系图逐表升级为版本二，断言 Doctor 分类完整且事件绝不伪装成 schema 一
async def test_reconcile_preserves_and_blocks_future_runtime_record_schemas(
    tmp_path: Path,
) -> None:
    reconciler, runtime, _sessions = _reconciler(tmp_path)
    now = datetime(2026, 8, 18, tzinfo=UTC)
    runtime.create_thread(
        ThreadRecord(
            id="thread-future-rows",
            title="future",
            workspace=str(tmp_path),
            created_at=now,
            updated_at=now,
        )
    )
    runtime.create_turn(
        TurnRecord(
            id="turn-future-rows",
            thread_id="thread-future-rows",
            status=TurnStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
    )
    runtime.record_item_and_event(
        TurnItemRecord(
            id="item-future-rows",
            turn_id="turn-future-rows",
            kind=TurnItemKind.MESSAGE,
            created_at=now,
        ),
        event_type="test.future",
        event_payload={},
        event_ts=now,
    )
    runtime.upsert_session_facade(
        SessionFacadeRecord(thread_id="thread-future-rows", mode="chat")
    )
    connection = sqlite3.connect(runtime.path)
    for table in (
        "runtime_threads",
        "runtime_turns",
        "runtime_turn_items",
        "runtime_events",
        "runtime_session_facades",
    ):
        connection.execute(f"UPDATE {table} SET schema_version = 2")
    connection.commit()
    connection.close()

    before = reconciler.inspect()
    after = await reconciler.repair()
    codes = {issue.code for issue in before.issues}

    assert {
        "unsupported_runtime_thread_schema",
        "unsupported_runtime_turn_schema",
        "unsupported_runtime_item_schema",
        "unsupported_runtime_event_schema",
        "unsupported_runtime_facade_schema",
    }.issubset(codes)
    assert before.healthy is False
    assert after.repaired == []
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.get_thread("thread-future-rows")
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.get_turn("turn-future-rows")
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.list_items("turn-future-rows")
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.list_events("thread-future-rows")
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.get_session_facade("thread-future-rows")
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.upsert_thread(
            ThreadRecord(
                id="thread-future-rows",
                title="must not overwrite future",
                workspace=str(tmp_path),
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        runtime.append_event(
            thread_id="thread-future-rows",
            turn_id="turn-future-rows",
            event_type="test.must-not-append",
            payload={},
            ts=now,
        )
    service = RuntimeService(runtime, workspace=tmp_path)
    with pytest.raises(UnsupportedRuntimeRecordSchemaError):
        await service.list_events("thread-future-rows")
    connection = sqlite3.connect(runtime.path)
    versions = {
        table: connection.execute(
            f"SELECT DISTINCT schema_version FROM {table}"
        ).fetchone()[0]
        for table in (
            "runtime_threads",
            "runtime_turns",
            "runtime_turn_items",
            "runtime_events",
            "runtime_session_facades",
        )
    }
    connection.close()
    assert set(versions.values()) == {2}

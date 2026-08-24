from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import pytest

from code_rook.core import app as core_app
from code_rook.core.daemon_lock import DaemonLock, DaemonLockError
from code_rook.core.upgrade import (
    UpgradeBackupIntegrityError,
    UpgradeBackupManager,
    UpgradeStateLockError,
    ensure_v1_upgrade_backup,
    inspect_v1_upgrade_backup,
)


# 功能：验证 v1 升级前只备份受控用户状态且明确忽略环境文件
# 设计：同时放置 routes、session 和 .env，检查 manifest 与副本锁定凭据边界
def test_upgrade_backup_copies_only_controlled_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "sessions" / "s1").mkdir(parents=True)
    (state / "config.toml").write_text("[llm]\n", encoding="utf-8")
    (state / "routes.json").write_text("{}", encoding="utf-8")
    (state / "sessions" / "s1" / "meta.json").write_text("{}", encoding="utf-8")
    (state / ".env").write_text("API_KEY=secret", encoding="utf-8")

    record = UpgradeBackupManager(state).ensure_backup("provider-catalog-v1")
    backup = Path(record.backup_dir)

    assert set(record.entries) == {"config.toml", "routes.json", "sessions"}
    assert (backup / "config.toml").is_file()
    assert (backup / "routes.json").is_file()
    assert (backup / "sessions" / "s1" / "meta.json").is_file()
    assert not (backup / ".env").exists()


# 功能：验证相同迁移重复执行会返回同一备份而不是持续复制用户数据
# 设计：首次备份后修改源文件再调用，断言路径和备份正文均保持第一次快照
def test_upgrade_backup_is_idempotent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    route_file = state / "routes.json"
    route_file.write_text('{"version": 1}', encoding="utf-8")
    manager = UpgradeBackupManager(state)

    first = manager.ensure_backup("provider-catalog-v1")
    route_file.write_text('{"version": 2}', encoding="utf-8")
    second = manager.ensure_backup("provider-catalog-v1")

    assert first == second
    assert (Path(first.backup_dir) / "routes.json").read_text(encoding="utf-8") == (
        '{"version": 1}'
    )


# 功能：验证损坏的迁移标记阻断升级且永远不会被事后快照替换
# 设计：预置非法 JSON 并调用真实备份入口，检查原证据、invalid 状态和备份目录均不变化
def test_corrupt_upgrade_marker_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    marker_root = state / "migrations"
    marker_root.mkdir(parents=True)
    marker = marker_root / "provider-catalog-v1.json"
    marker.write_text("not-json", encoding="utf-8")

    manager = UpgradeBackupManager(state)
    with pytest.raises(UpgradeBackupIntegrityError, match="marker is invalid"):
        manager.ensure_backup("provider-catalog-v1")

    assert marker.read_text(encoding="utf-8") == "not-json"
    assert manager.inspect_backup("provider-catalog-v1") == "invalid"
    assert not (state / "backups").exists()
    assert not list(marker_root.glob("provider-catalog-v1.corrupt-*.json"))


# 功能：验证只读备份检查把符号链接目录报告为 invalid 且不向外部写入
# 设计：让 backups 指向外部哨兵并调用公共 inspect，断言返回稳定状态而不是崩溃或创建文件
def test_inspect_upgrade_backup_reports_symlinked_directory_invalid(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    try:
        os.symlink(outside, state / "backups", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this host")

    assert inspect_v1_upgrade_backup(state) == "invalid"
    assert sentinel.read_text(encoding="utf-8") == "keep"


# 功能：验证 SQLite WAL 中已提交但未 checkpoint 的数据进入一致升级备份
# 设计：保持源连接开启并关闭自动 checkpoint，再从备份数据库查询完整行集
def test_upgrade_backup_uses_sqlite_consistent_snapshot(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    runtime_path = state / "runtime.db"
    source = sqlite3.connect(runtime_path)
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, payload TEXT)")
        source.executemany(
            "INSERT INTO events(payload) VALUES (?)",
            [("first",), ("second",)],
        )
        source.commit()
        assert (state / "runtime.db-wal").is_file()

        record = UpgradeBackupManager(state).ensure_backup("provider-catalog-v1")
    finally:
        source.close()

    backup = Path(record.backup_dir)
    copied = sqlite3.connect(backup / "runtime.db")
    try:
        rows = copied.execute("SELECT payload FROM events ORDER BY seq").fetchall()
    finally:
        copied.close()
    assert rows == [("first",), ("second",)]
    assert "runtime.db" in record.entries
    assert "runtime.db-wal" not in record.entries
    assert not (backup / "runtime.db-wal").exists()


# 功能：验证 manifest 与 marker 不一致时禁止以当前状态重建所谓迁移前快照
# 设计：篡改首次 manifest 后再次执行，断言硬失败且 marker 仍指向原备份证据
def test_mismatched_upgrade_manifest_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "routes.json").write_text("{}", encoding="utf-8")
    manager = UpgradeBackupManager(state)
    first = manager.ensure_backup("provider-catalog-v1")
    (Path(first.backup_dir) / "manifest.json").write_text("{}", encoding="utf-8")

    marker = state / "migrations" / "provider-catalog-v1.json"
    original_marker = marker.read_bytes()
    with pytest.raises(UpgradeBackupIntegrityError, match="marker is invalid"):
        manager.ensure_backup("provider-catalog-v1")

    assert marker.read_bytes() == original_marker
    assert manager.inspect_backup("provider-catalog-v1") == "invalid"
    assert len(list((state / "backups").glob("pre-provider-catalog-v1-*"))) == 1
    assert not list((state / "migrations").glob("*.corrupt-*.json"))


# 功能：验证备份正文被篡改时 marker 与 manifest 即使一致也不能继续冒充可信快照
# 设计：创建含 route 的真实备份后只改副本正文，检查只读 inspect 与再次 ensure 都按 digest 失败关闭
def test_tampered_backup_content_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "routes.json").write_text('{"before": true}', encoding="utf-8")
    manager = UpgradeBackupManager(state)
    record = manager.ensure_backup("provider-catalog-v1")
    backup_route = Path(record.backup_dir) / "routes.json"
    backup_route.write_text('{"after": true}', encoding="utf-8")

    assert manager.inspect_backup("provider-catalog-v1") == "invalid"
    with pytest.raises(UpgradeBackupIntegrityError, match="marker is invalid"):
        manager.ensure_backup("provider-catalog-v1")

    assert backup_route.read_text(encoding="utf-8") == '{"after": true}'


# 功能：验证迁移备份只读检查能区分缺失、有效与损坏且不移动证据文件
# 设计：依次观察创建前、创建后和篡改 manifest 后状态，确认 Doctor 检查无副作用
def test_upgrade_backup_inspection_is_read_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    manager = UpgradeBackupManager(state)

    assert manager.inspect_backup("provider-catalog-v1") == "missing"
    record = manager.ensure_backup("provider-catalog-v1")
    assert manager.inspect_backup("provider-catalog-v1") == "valid"
    (Path(record.backup_dir) / "manifest.json").write_text("{}", encoding="utf-8")
    assert manager.inspect_backup("provider-catalog-v1") == "invalid"
    assert (state / "migrations" / "provider-catalog-v1.json").is_file()
    assert not list((state / "migrations").glob("*.corrupt-*.json"))


# 功能：验证用户状态互斥锁竞争会在读取或写入备份目标前立即失败关闭
# 设计：手动持有与生产入口相同的 OS 文件锁，再调用真实 v1 备份包装器并检查无 marker 产生
def test_upgrade_state_lock_conflict_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    lock = DaemonLock(state / "state-mutation.lock")
    lock.acquire()
    try:
        with pytest.raises(UpgradeStateLockError, match="state mutation"):
            ensure_v1_upgrade_backup(state)
    finally:
        lock.release()

    assert not (state / "migrations" / "provider-catalog-v1.json").exists()


# 功能：验证 daemon 持有单写者锁后先备份再迁移，最后才进入异步运行时
# 设计：用轻量替身记录同步入口顺序，并让真实 asyncio.run 执行空协程
def test_daemon_startup_locks_backup_before_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeLock:
        # 初始化不触碰真实用户状态目录
        def __init__(self, path: Path) -> None:
            calls.append(f"lock-init:{path.name}")

        # 记录启动互斥锁获取时点
        def acquire(self) -> None:
            calls.append("lock-acquire")

        # 记录入口退出时释放启动互斥锁
        def release(self) -> None:
            calls.append("lock-release")

    class FakeApp:
        # 初始化可接收入口注入的已持有锁
        def __init__(self, *, env_file: Path | None = None) -> None:
            assert env_file is None
            self._daemon_lock: FakeLock | None = None

        # 模拟 daemon 异步主循环并记录其启动时点
        async def run(self) -> None:
            assert self._daemon_lock is not None
            calls.append("app-run")

    monkeypatch.setattr(core_app, "DaemonLock", FakeLock)
    monkeypatch.setattr(
        core_app,
        "ensure_v1_upgrade_backup",
        lambda: calls.append("backup"),
    )
    monkeypatch.setattr(
        core_app,
        "migrate_legacy_state",
        lambda: calls.append("migrate"),
    )
    monkeypatch.setattr(core_app, "CoreApp", FakeApp)
    monkeypatch.setattr(sys, "argv", ["coderook-core"])

    core_app.run()

    assert calls == [
        "lock-init:core.lock",
        "lock-acquire",
        "backup",
        "migrate",
        "app-run",
        "lock-release",
    ]


# 功能：验证升级备份失败时不运行任何迁移且必定释放 daemon 单写者锁
# 设计：令备份抛出异常并用调用序列断言 fail-closed 与 finally 清理语义
def test_daemon_startup_backup_failure_blocks_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeLock:
        # 初始化测试锁替身
        def __init__(self, _path: Path) -> None:
            return

        # 记录成功获取入口互斥锁
        def acquire(self) -> None:
            calls.append("lock-acquire")

        # 记录异常路径仍释放入口互斥锁
        def release(self) -> None:
            calls.append("lock-release")

    # 模拟无法形成可信备份的硬失败
    def fail_backup() -> None:
        calls.append("backup")
        raise OSError("disk full")

    monkeypatch.setattr(core_app, "DaemonLock", FakeLock)
    monkeypatch.setattr(core_app, "ensure_v1_upgrade_backup", fail_backup)
    monkeypatch.setattr(
        core_app,
        "migrate_legacy_state",
        lambda: calls.append("migrate"),
    )
    monkeypatch.setattr(sys, "argv", ["coderook-core"])

    with pytest.raises(OSError, match="disk full"):
        core_app.run()

    assert calls == ["lock-acquire", "backup", "lock-release"]


# 功能：验证已有 daemon 持锁时新进程不会读取备份目标或执行任何迁移
# 设计：让锁替身在 acquire 直接报告竞争，断言同步入口立即退出且无后续调用
def test_daemon_startup_lock_conflict_prevents_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class BusyLock:
        # 初始化竞争锁替身
        def __init__(self, _path: Path) -> None:
            return

        # 模拟另一个存活 daemon 已持有同一状态锁
        def acquire(self) -> None:
            calls.append("lock-acquire")
            raise DaemonLockError("already running")

    monkeypatch.setattr(core_app, "DaemonLock", BusyLock)
    monkeypatch.setattr(
        core_app,
        "ensure_v1_upgrade_backup",
        lambda: calls.append("backup"),
    )
    monkeypatch.setattr(
        core_app,
        "migrate_legacy_state",
        lambda: calls.append("migrate"),
    )
    monkeypatch.setattr(sys, "argv", ["coderook-core"])

    with pytest.raises(SystemExit, match="already running"):
        core_app.run()

    assert calls == ["lock-acquire"]

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from code_rook.core.daemon_lock import DaemonLock, DaemonLockError
from code_rook.core.state_paths import (
    StatePathSecurityError,
    secure_state_subdirectory,
    secure_user_state_root,
)

_MIGRATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_BACKUP_TARGETS = (
    "config.toml",
    "routes.json",
    "credentials.json",
    "sessions",
    "goals",
    "runtime.db",
    "fleet.db",
    "workflow.db",
)
_SQLITE_TARGETS = frozenset({"runtime.db", "fleet.db", "workflow.db"})


class UpgradeBackupRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    migration_id: str
    created_at: str
    backup_dir: str
    entries: tuple[str, ...] = Field(default_factory=tuple)
    entry_digests: dict[str, str] = Field(default_factory=dict)


class UpgradeBackupIntegrityError(RuntimeError):
    pass


class UpgradeStateLockError(RuntimeError):
    pass


class UpgradeBackupManager:
    # 初始化用户状态根目录和受控备份目录
    def __init__(self, state_root: Path | None = None, *, create: bool = True) -> None:
        safe_root = secure_user_state_root(
            state_root or Path("~/.coderook"),
            create=create,
        )
        self._state_root = safe_root.resolve(strict=True) if safe_root.exists() else safe_root
        self._backup_root = secure_state_subdirectory(
            self._state_root,
            "backups",
            create=False,
        )
        self._marker_root = secure_state_subdirectory(
            self._state_root,
            "migrations",
            create=False,
        )

    # 为指定迁移创建一次性一致备份，已有有效标记时幂等返回原记录
    def ensure_backup(self, migration_id: str) -> UpgradeBackupRecord:
        normalized = migration_id.strip().casefold()
        if not _MIGRATION_ID.fullmatch(normalized):
            raise ValueError("invalid migration id")
        marker = self._marker_root / f"{normalized}.json"
        existing = self._read_marker(marker)
        if existing is not None:
            return existing

        self._backup_root = secure_state_subdirectory(
            self._state_root,
            "backups",
            create=True,
        )
        self._marker_root = secure_state_subdirectory(
            self._state_root,
            "migrations",
            create=True,
        )
        marker = self._marker_root / f"{normalized}.json"

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        suffix = secrets.token_hex(4)
        final_dir = self._backup_root / f"pre-{normalized}-{timestamp}-{suffix}"
        staging = self._backup_root / f".{final_dir.name}.tmp"
        self._backup_root.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=False, exist_ok=False)
        entries: list[str] = []
        try:
            for name in _BACKUP_TARGETS:
                source = self._state_root / name
                if not source.exists() or source.is_symlink():
                    continue
                destination = staging / name
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=True)
                elif name in _SQLITE_TARGETS and source.is_file():
                    self._backup_sqlite(source, destination)
                elif source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination, follow_symlinks=False)
                else:
                    continue
                entries.append(name)
            record = UpgradeBackupRecord(
                migration_id=normalized,
                created_at=datetime.now(UTC).isoformat(),
                backup_dir=str(final_dir),
                entries=tuple(entries),
                entry_digests={name: self._digest_entry(staging / name) for name in entries},
            )
            (staging / "manifest.json").write_text(
                record.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            self._restrict_permissions(staging)
            staging.replace(final_dir)
            self._write_marker(marker, record)
        except Exception:
            if staging.exists() and staging.parent == self._backup_root:
                shutil.rmtree(staging)
            raise
        return record

    # 只读检查指定迁移备份标记及 manifest 是否一致，不创建或隔离任何文件
    def inspect_backup(self, migration_id: str) -> Literal["missing", "valid", "invalid"]:
        normalized = migration_id.strip().casefold()
        if not _MIGRATION_ID.fullmatch(normalized):
            raise ValueError("invalid migration id")
        marker = self._marker_root / f"{normalized}.json"
        if not marker.exists():
            return "missing"
        try:
            self._load_marker(marker)
        except (OSError, UnicodeError, ValueError, ValidationError):
            return "invalid"
        return "valid"

    # 使用 SQLite 在线备份 API 复制含 WAL 已提交页的一致数据库快照
    def _backup_sqlite(self, source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        source_db = sqlite3.connect(source_uri, uri=True, timeout=5.0)
        destination_db = sqlite3.connect(destination, timeout=5.0)
        try:
            source_db.backup(destination_db)
            result = destination_db.execute("PRAGMA quick_check").fetchone()
            if result is None or result[0] != "ok":
                raise sqlite3.DatabaseError("backup database failed quick_check")
            destination_db.commit()
        finally:
            destination_db.close()
            source_db.close()

    # 将备份目录和普通文件权限收紧，且不跟随内部符号链接
    def _restrict_permissions(self, root: Path) -> None:
        os.chmod(root, 0o700)
        for path in root.rglob("*"):
            if path.is_symlink():
                continue
            os.chmod(path, 0o700 if path.is_dir() else 0o600)

    # 计算普通文件、目录树和保留符号链接目标的确定性 SHA-256 摘要
    def _digest_entry(self, root: Path) -> str:
        digest = hashlib.sha256()

        # 将单个节点类型、相对路径与内容加入摘要且不跟随符号链接
        def update(path: Path, relative: str) -> None:
            encoded = relative.encode("utf-8", errors="surrogatepass")
            if path.is_symlink():
                target = os.readlink(path).encode("utf-8", errors="surrogatepass")
                digest.update(b"L" + len(encoded).to_bytes(8, "big") + encoded)
                digest.update(len(target).to_bytes(8, "big") + target)
                return
            if path.is_dir():
                digest.update(b"D" + len(encoded).to_bytes(8, "big") + encoded)
                for child in sorted(path.iterdir(), key=lambda item: item.name):
                    child_relative = f"{relative}/{child.name}" if relative else child.name
                    update(child, child_relative)
                return
            if not path.is_file():
                raise ValueError("backup contains an unsupported filesystem entry")
            content_digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    content_digest.update(chunk)
            digest.update(b"F" + len(encoded).to_bytes(8, "big") + encoded)
            digest.update(content_digest.digest())

        update(root, "")
        return digest.hexdigest()

    # 读取并验证迁移标记，损坏或越界记录一律失败关闭且保留原证据
    def _read_marker(self, marker: Path) -> UpgradeBackupRecord | None:
        if not marker.exists():
            return None
        try:
            return self._load_marker(marker)
        except (OSError, UnicodeError, ValueError, ValidationError) as exc:
            raise UpgradeBackupIntegrityError(
                "upgrade backup marker is invalid; refusing to create a "
                "post-migration snapshot under the same migration id"
            ) from exc

    # 严格读取迁移 marker 与备份 manifest 并拒绝越界或不完整记录
    def _load_marker(self, marker: Path) -> UpgradeBackupRecord:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError("backup marker must be a regular file")
        record = UpgradeBackupRecord.model_validate_json(marker.read_text(encoding="utf-8"))
        declared_backup = Path(record.backup_dir)
        if declared_backup.is_symlink():
            raise ValueError("backup directory must not be a symlink")
        backup_dir = declared_backup.resolve()
        if not backup_dir.is_relative_to(self._backup_root.resolve()):
            raise ValueError("backup marker points outside backup root")
        if not backup_dir.is_dir():
            raise ValueError("backup directory is missing")
        manifest = backup_dir / "manifest.json"
        if not manifest.is_file() or manifest.is_symlink():
            raise ValueError("backup manifest is missing")
        manifest_record = UpgradeBackupRecord.model_validate_json(
            manifest.read_text(encoding="utf-8")
        )
        if manifest_record != record:
            raise ValueError("backup marker and manifest disagree")
        if record.migration_id != marker.stem:
            raise ValueError("backup marker migration id disagrees with filename")
        if len(set(record.entries)) != len(record.entries):
            raise ValueError("backup manifest contains duplicate entries")
        if set(record.entry_digests) != set(record.entries):
            raise ValueError("backup manifest digest set disagrees with entries")
        for entry in record.entries:
            if entry not in _BACKUP_TARGETS:
                raise ValueError("backup manifest contains an unknown entry")
            target = backup_dir / entry
            if not target.exists() or target.is_symlink():
                raise ValueError("backup manifest entry is missing")
            actual_digest = self._digest_entry(target)
            if not hmac.compare_digest(record.entry_digests[entry], actual_digest):
                raise ValueError("backup manifest entry digest disagrees with content")
        return record

    # 使用原子替换写入权限收紧的迁移完成标记
    def _write_marker(self, marker: Path, record: UpgradeBackupRecord) -> None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(marker.parent, 0o700)
        temporary = marker.with_suffix(f"{marker.suffix}.{secrets.token_hex(4)}.tmp")
        temporary.write_text(
            record.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(marker)
        os.chmod(marker, 0o600)


class V1StateMutation:
    # 初始化覆盖升级备份与后续用户状态写入的短时跨进程互斥区
    def __init__(self, state_root: Path | None = None) -> None:
        safe_root = secure_user_state_root(
            state_root or Path("~/.coderook"),
            create=True,
        )
        self._state_root = safe_root.resolve(strict=True)
        self._lock = DaemonLock(self._state_root / "state-mutation.lock")
        self._record: UpgradeBackupRecord | None = None

    # 获取用户状态互斥锁并在任何调用方写入前形成可信升级快照
    def __enter__(self) -> UpgradeBackupRecord:
        try:
            self._lock.acquire()
        except DaemonLockError as exc:
            raise UpgradeStateLockError(
                "another CodeRook state mutation is in progress; retry after it finishes"
            ) from exc
        try:
            self._record = UpgradeBackupManager(self._state_root).ensure_backup(
                "provider-catalog-v1"
            )
        except BaseException:
            self._lock.release()
            raise
        return self._record

    # 无论写入成功或失败都释放短时用户状态互斥锁
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del exc_type, exc_value, traceback
        self._lock.release()
        return False


# 返回同时覆盖 v1 前置备份和单次用户状态写入的互斥上下文
def v1_state_mutation(state_root: Path | None = None) -> V1StateMutation:
    return V1StateMutation(state_root)


# 执行 v1 Provider Catalog 前置备份并返回可供日志展示的脱敏记录
def ensure_v1_upgrade_backup(
    state_root: Path | None = None,
) -> UpgradeBackupRecord:
    with v1_state_mutation(state_root) as record:
        return record


# 只读返回 v1 Provider Catalog 前置备份的可审计状态
def inspect_v1_upgrade_backup(
    state_root: Path | None = None,
) -> Literal["missing", "valid", "invalid"]:
    try:
        return UpgradeBackupManager(state_root, create=False).inspect_backup(
            "provider-catalog-v1"
        )
    except (OSError, StatePathSecurityError, ValueError):
        return "invalid"

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateFileFingerprint:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    digest: str


# 不跟随符号链接读取状态文件身份与内容摘要，供显式 repair 防止竞态误隔离
def fingerprint_state_file(path: Path) -> StateFileFingerprint:
    source = path.absolute()
    initial = os.lstat(source)
    digest = hashlib.sha256()
    if stat.S_ISLNK(initial.st_mode):
        digest.update(os.readlink(source).encode("utf-8", errors="surrogatepass"))
    elif stat.S_ISREG(initial.st_mode):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino, opened.st_mode) != (
                initial.st_dev,
                initial.st_ino,
                initial.st_mode,
            ):
                raise OSError("state file identity changed while fingerprinting")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        finally:
            os.close(descriptor)
    else:
        raise OSError("state record is not a regular file or symbolic link")
    return StateFileFingerprint(
        device=initial.st_dev,
        inode=initial.st_ino,
        mode=initial.st_mode,
        size=initial.st_size,
        modified_ns=initial.st_mtime_ns,
        digest=digest.hexdigest(),
    )


# 在不覆盖并发新文件的前提下恢复被竞态移动的原记录
def _restore_without_overwrite(destination: Path, source: Path) -> bool:
    try:
        if destination.is_symlink():
            os.symlink(os.readlink(destination), source)
        else:
            os.link(destination, source, follow_symlinks=False)
    except (FileExistsError, NotImplementedError, OSError):
        return False
    destination.unlink()
    return True


# 把单条损坏状态文件移入同目录隔离区并追加不含原始内容的诊断记录
def quarantine_invalid_file(
    path: Path,
    *,
    category: str,
    reason: str,
    state_root: Path,
    expected_fingerprint: StateFileFingerprint | None = None,
) -> Path | None:
    try:
        source = path.absolute()
        current_fingerprint = fingerprint_state_file(source)
        if (
            expected_fingerprint is not None
            and current_fingerprint != expected_fingerprint
        ):
            logger.warning("refuse to quarantine changed %s state: %s", category, path)
            return None
        resolved_root = state_root.resolve(strict=True)
        resolved_parent = source.parent.resolve(strict=True)
        if not resolved_parent.is_relative_to(resolved_root):
            logger.warning("refuse to quarantine %s outside state root: %s", category, path)
            return None
        quarantine_dir = source.parent / "_quarantine"
        if quarantine_dir.is_symlink():
            logger.warning("refuse symlinked quarantine directory: %s", quarantine_dir)
            return None
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        if (
            quarantine_dir.is_symlink()
            or not quarantine_dir.resolve(strict=True).is_relative_to(resolved_root)
        ):
            logger.warning("refuse unsafe quarantine directory: %s", quarantine_dir)
            return None
        journal = quarantine_dir / "quarantine.jsonl"
        if journal.is_symlink() or (
            journal.exists()
            and (
                not journal.is_file()
                or not journal.resolve(strict=True).is_relative_to(resolved_root)
            )
        ):
            logger.warning("refuse unsafe quarantine journal: %s", journal)
            return None
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = quarantine_dir / (
            f"{source.stem}.{timestamp}.{secrets.token_hex(3)}.invalid{source.suffix}"
        )
        os.replace(source, destination)
        if expected_fingerprint is not None:
            moved_fingerprint = fingerprint_state_file(destination)
            if moved_fingerprint != expected_fingerprint:
                restored = _restore_without_overwrite(destination, source)
                logger.warning(
                    "%s %s state changed during quarantine: %s",
                    "restored" if restored else "preserved concurrent",
                    category,
                    path,
                )
                return None
        record = {
            "schema_version": 1,
            "ts": datetime.now(UTC).isoformat(),
            "category": category,
            "original_name": source.name,
            "quarantined_name": destination.name,
            "reason": reason,
        }
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(journal, flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError("quarantine journal is not a regular file")
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return destination
    except (OSError, RuntimeError):
        logger.warning("failed to quarantine invalid %s state: %s", category, path)
        return None


# 统计状态根目录下仍可供 Doctor 报告的隔离记录数量
def count_quarantined_records(state_root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        resolved_root = state_root.resolve(strict=True)
    except (OSError, RuntimeError):
        return counts
    for journal in state_root.glob("**/_quarantine/quarantine.jsonl"):
        try:
            if journal.is_symlink() or not journal.resolve(strict=True).is_relative_to(
                resolved_root
            ):
                continue
            lines = journal.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            if not line:
                continue
            category = "unknown"
            try:
                payload = json.loads(line)
                candidate = payload.get("category") if isinstance(payload, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    category = candidate.strip()
            except json.JSONDecodeError:
                pass
            counts[category] = counts.get(category, 0) + 1
    return counts

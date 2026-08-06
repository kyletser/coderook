"""跨进程 daemon 单写者锁，防止两个 coderook-core 同时管理同一个状态目录。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class DaemonLockError(RuntimeError):
    """另一个存活的 daemon 已持有同一状态目录的单写者锁。"""


# 锁定区域的固定字节偏移；文件头部保留给持有者 PID 文本
_LOCK_OFFSET = 512


# 读取锁文件中记录的持有者 PID（不存在或损坏时返回 None）
def _read_holder_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError, UnicodeError):
        return None


class DaemonLock:
    """进程退出即自动释放的 OS 级文件锁（POSIX flock / Windows msvcrt.locking）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    # 获取排他锁；已被其他存活进程持有时抛出 DaemonLockError
    def acquire(self) -> None:
        if self._fd is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            self._lock_fd(fd)
        except OSError:
            holder = _read_holder_pid(self._path)
            os.close(fd)
            detail = f" (holder pid={holder})" if holder is not None else ""
            raise DaemonLockError(
                f"another coderook-core is already managing {self._path.parent}{detail}"
            ) from None
        old_size = os.fstat(fd).st_size
        content = f"{os.getpid()}\n".encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, content)
        if old_size > len(content):
            os.write(fd, b" " * (old_size - len(content)))
        os.fsync(fd)
        self._fd = fd

    # 释放锁；未持有时为空操作
    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            self._unlock_fd(fd)
        except OSError:
            pass
        os.close(fd)

    # 按平台对 fd 尝试非阻塞排他锁，冲突时抛出 OSError
    def _lock_fd(self, fd: int) -> None:
        if sys.platform == "win32":
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    # 按平台解除 fd 上的排他锁
    def _unlock_fd(self, fd: int) -> None:
        if sys.platform == "win32":
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)

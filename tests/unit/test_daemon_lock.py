"""daemon_lock 单写者锁的单元测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_rook.core.daemon_lock import DaemonLock, DaemonLockError


# 功能：acquire 成功后锁文件记录当前进程 PID
# 设计：直接读文件断言 PID，验证锁文件可被运维工具用于诊断持有者
def test_acquire_writes_holder_pid(tmp_path: Path) -> None:
    lock = DaemonLock(tmp_path / "core.lock")
    lock.acquire()
    try:
        assert lock.path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


# 功能：第二个锁实例对同一文件 acquire 必须失败并报出持有者 PID
# 设计：同进程内开第二个 fd 模拟第二个 daemon；错误信息包含 pid 便于用户定位冲突进程
def test_second_acquire_raises_with_holder_pid(tmp_path: Path) -> None:
    first = DaemonLock(tmp_path / "core.lock")
    first.acquire()
    try:
        second = DaemonLock(tmp_path / "core.lock")
        with pytest.raises(DaemonLockError) as exc_info:
            second.acquire()
        assert str(os.getpid()) in str(exc_info.value)
    finally:
        first.release()


# 功能：release 之后同一文件可以被重新获取
# 设计：覆盖 acquire→release→acquire 全周期，防止释放逻辑残留锁状态导致 daemon 无法重启
def test_release_allows_reacquire(tmp_path: Path) -> None:
    lock = DaemonLock(tmp_path / "core.lock")
    lock.acquire()
    lock.release()
    again = DaemonLock(tmp_path / "core.lock")
    again.acquire()
    again.release()


# 功能：未 acquire 时 release 与重复 acquire 都是安全的空操作
# 设计：关闭序列可能无条件调用 release，必须容忍未持锁状态；重复 acquire 不应破坏已持有的锁
def test_release_without_acquire_is_noop(tmp_path: Path) -> None:
    lock = DaemonLock(tmp_path / "core.lock")
    lock.release()
    lock.acquire()
    lock.acquire()
    lock.release()

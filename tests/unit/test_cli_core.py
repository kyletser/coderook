from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from code_rook.cli.commands import core
from code_rook.core.config import CodeRookConfig


# 功能：验证 PID 探测能识别当前进程并拒绝不存在的极大 PID
# 设计：使用当前测试进程避免派生子进程，同时用平台通用的无效 PID 覆盖失败路径
def test_pid_exists_detects_current_process_and_missing_pid() -> None:
    assert core._pid_exists(os.getpid())
    assert not core._pid_exists(2_147_483_647)


# 功能：验证 Core 已就绪时 ensure_core_running 直接复用且不派生新进程
# 设计：替换 readiness 探针并让 spawn 在误调用时立刻失败，精确覆盖单实例复用语义
def test_ensure_core_running_reuses_ready_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_core_ready", lambda config: True)
    spawn = MagicMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(core, "_spawn_core", spawn)

    assert core.ensure_core_running(CodeRookConfig()) is False
    spawn.assert_not_called()


# 功能：验证 Core 未运行时 ensure_core_running 启动后台进程并等待到认证就绪
# 设计：用 readiness 序列模拟启动前和启动后的状态，避免测试依赖真实端口与子进程
def test_ensure_core_running_spawns_and_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readiness = iter([False, False, True])
    monkeypatch.setattr(core, "_core_ready", lambda config: next(readiness))

    async def port_closed(config: CodeRookConfig) -> bool:
        return False

    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(core, "_port_open", port_closed)
    monkeypatch.setattr(core, "_spawn_core", lambda: proc)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    assert core.ensure_core_running(CodeRookConfig(), timeout_s=1.0) is True

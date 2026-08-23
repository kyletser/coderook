from __future__ import annotations

import os
from pathlib import Path
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
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda config: {"workspace": str(Path.cwd()), "active_runs": 0},
    )
    spawn = MagicMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(core, "_spawn_core", spawn)

    assert core.ensure_core_running(CodeRookConfig()) is False
    spawn.assert_not_called()


# 功能：验证 Core 未运行时 ensure_core_running 启动后台进程并等待到认证就绪
# 设计：用 readiness 序列模拟启动前和启动后的状态，避免测试依赖真实端口与子进程
def test_ensure_core_running_spawns_and_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = iter(
        [
            None,
            None,
            {"workspace": str(Path.cwd()), "active_runs": 0},
        ]
    )
    monkeypatch.setattr(core, "_core_metadata", lambda config: next(metadata))

    async def port_closed(config: CodeRookConfig) -> bool:
        return False

    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(core, "_port_open", port_closed)
    monkeypatch.setattr(core, "_spawn_core", lambda: proc)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    assert core.ensure_core_running(CodeRookConfig(), timeout_s=1.0) is True


# 功能：验证空闲 daemon 绑定其他 workspace 时启动器会有序重启到当前目录
# 设计：模拟旧 workspace 元数据、受管 PID 和成功关闭，再让新进程返回当前 workspace，覆盖安全切换路径
def test_ensure_core_running_switches_idle_managed_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.chdir(current)
    metadata = iter(
        [
            {"workspace": str(tmp_path / "other"), "active_runs": 0},
            {"workspace": str(current), "active_runs": 0},
        ]
    )
    monkeypatch.setattr(core, "_core_metadata", lambda _config: next(metadata))
    monkeypatch.setattr(core, "_running_pid", lambda: 1234)
    stopped = MagicMock(return_value=True)
    monkeypatch.setattr(core, "stop_core", stopped)

    async def port_closed(_config: CodeRookConfig) -> bool:
        return False

    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(core, "_port_open", port_closed)
    monkeypatch.setattr(core, "_spawn_core", lambda: proc)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    assert core.ensure_core_running(CodeRookConfig(), timeout_s=1.0) is True
    stopped.assert_called_once()


# 功能：验证其他 workspace 仍有活动 run 时启动器拒绝切换 daemon
# 设计：返回 active_runs=1 并把 stop 替换为误调用失败，确保不会中断另一仓库正在执行的工作
def test_ensure_core_running_refuses_busy_workspace_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(tmp_path / "other"), "active_runs": 1},
    )
    stop = MagicMock(side_effect=AssertionError("must not stop a busy daemon"))
    monkeypatch.setattr(core, "stop_core", stop)

    with pytest.raises(core.CoreLaunchError, match="busy in another workspace"):
        core.ensure_core_running(CodeRookConfig())
    stop.assert_not_called()


# 功能：验证手动 Core 模式只允许连接当前 workspace
# 设计：分别返回当前和其他目录的元数据，证明校验函数不做进程切换且会拒绝错误仓库
def test_validate_core_workspace_rejects_other_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(current), "active_runs": 0},
    )

    core.validate_core_workspace(CodeRookConfig())

    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(tmp_path / "other"), "active_runs": 0},
    )
    with pytest.raises(core.CoreLaunchError, match="another workspace"):
        core.validate_core_workspace(CodeRookConfig())

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.cli.commands import core
from code_rook.core.config import CodeRookConfig


# 功能：验证 PID 探测能识别当前进程并拒绝不存在的极大 PID
# 设计：使用当前测试进程避免派生子进程，同时用平台通用的无效 PID 覆盖失败路径
def test_pid_exists_detects_current_process_and_missing_pid() -> None:
    assert core._pid_exists(os.getpid())
    assert not core._pid_exists(2_147_483_647)


# 功能：验证 Windows 探测连接被 Core 主动重置时仍判定端口已打开
# 设计：让 wait_closed 抛出真实 WinError 对应异常，确认关闭阶段不打断 restart 流程
def test_port_open_ignores_reset_while_closing_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = MagicMock()
    writer.wait_closed = AsyncMock(side_effect=ConnectionResetError(10054, "reset"))
    monkeypatch.setattr(
        core.asyncio,
        "open_connection",
        AsyncMock(return_value=(object(), writer)),
    )

    assert asyncio.run(core._port_open(CodeRookConfig())) is True
    writer.close.assert_called_once_with()


# 功能：验证后台 Core 启动器不把用户项目目录保留为 Windows 进程工作目录。
# 设计：捕获 Popen 参数，确认项目通过显式参数传递且启动器驻留在用户状态目录。
def test_spawn_core_detaches_process_cwd_from_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(core, "_PID_FILE", state / "coderook-core.pid")
    captured: dict[str, object] = {}
    proc = MagicMock(pid=4321)

    # 捕获命令与启动选项而不派生真实后台进程
    def popen(command: list[str], **kwargs: object) -> MagicMock:
        captured["command"] = command
        captured.update(kwargs)
        return proc

    monkeypatch.setattr(core.subprocess, "Popen", popen)

    assert core._spawn_core() is proc
    assert captured["cwd"] == state.resolve()
    assert captured["command"] == [
        core.sys.executable,
        "-m",
        "code_rook.core",
        "--workspace",
        str(workspace.resolve()),
    ]
    assert (state / "coderook-core.pid").read_text(encoding="utf-8") == "4321"


# 功能：验证虚拟环境启动器 PID 失效后仍能从 daemon 锁文件恢复真实进程号
# 设计：让启动 PID 指向已退出进程、锁文件指向存活进程，覆盖 Windows 启动器转交子进程场景
def test_running_pid_falls_back_to_daemon_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "coderook-core.pid"
    lock_file = tmp_path / "core.lock"
    pid_file.write_text("1001", encoding="utf-8")
    lock_file.write_text("2002\n    ", encoding="utf-8")
    monkeypatch.setattr(core, "_PID_FILE", pid_file)
    monkeypatch.setattr(core, "_CORE_LOCK_FILE", lock_file)
    monkeypatch.setattr(core, "_pid_exists", lambda pid: pid == 2002)

    assert core._running_pid() == 2002
    assert not pid_file.exists()
    assert lock_file.exists()


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


# 功能：验证当前 workspace 已有受管 Core 时，显式 env 文件会强制重启并转发同一路径
# 设计：用先旧后新的元数据序列模拟重启，同时捕获 stop 和 spawn 入参排除静默复用
def test_explicit_env_restarts_same_workspace_managed_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "deployment.env"
    env_file.write_text("DEPLOYMENT_KEY=secret\n", encoding="utf-8")
    metadata = iter(
        [
            {"workspace": str(Path.cwd()), "active_runs": 0},
            {"workspace": str(Path.cwd()), "active_runs": 0},
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
    spawned = MagicMock(return_value=proc)
    monkeypatch.setattr(core, "_port_open", port_closed)
    monkeypatch.setattr(core, "_spawn_core", spawned)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    assert core.ensure_core_running(
        CodeRookConfig(),
        timeout_s=1.0,
        env_file=env_file,
    ) is True
    stopped.assert_called_once()
    spawned.assert_called_once_with(env_file)


# 功能：验证认证可用但 PID 文件缺失的空闲 Core 仍可通过 IPC 有序重启
# 设计：模拟 Windows 启动器 PID 已退出但 daemon 仍存活，确保不再要求用户手工清理进程
def test_explicit_env_restarts_authenticated_core_without_pid_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metadata = iter(
        [
            {"workspace": str(Path.cwd()), "active_runs": 0},
            {"workspace": str(Path.cwd()), "active_runs": 0},
        ]
    )
    monkeypatch.setattr(core, "_core_metadata", lambda _config: next(metadata))
    monkeypatch.setattr(core, "_running_pid", lambda: None)
    stop = MagicMock(return_value=True)
    proc = MagicMock()
    proc.poll.return_value = None
    spawn = MagicMock(return_value=proc)
    monkeypatch.setattr(core, "stop_core", stop)
    monkeypatch.setattr(core, "_spawn_core", spawn)

    async def port_closed(_config: CodeRookConfig) -> bool:
        return False

    monkeypatch.setattr(core, "_port_open", port_closed)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    env_file = tmp_path / "deployment.env"
    assert core.ensure_core_running(CodeRookConfig(), env_file=env_file) is True

    stop.assert_called_once()
    spawn.assert_called_once_with(env_file)


# 功能：验证 PID 文件缺失时 stop 仍通过已认证 IPC 关闭 Core
# 设计：让端口在一次轮询后关闭，证明优雅退出不依赖易失的启动器进程号
def test_stop_core_uses_authenticated_shutdown_without_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_running_pid", lambda: None)
    shutdown = MagicMock()

    async def request_shutdown(_config: CodeRookConfig) -> None:
        shutdown()

    ports = iter([True, False])

    async def port_open(_config: CodeRookConfig) -> bool:
        return next(ports)

    monkeypatch.setattr(core, "_request_shutdown", request_shutdown)
    monkeypatch.setattr(core, "_port_open", port_open)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    assert core.stop_core(CodeRookConfig(), timeout_s=1.0) is True
    shutdown.assert_called_once_with()


# 功能：验证受管 Core 停止后仍占用端口时不会被当作已携带 overlay 的新实例
# 设计：让 stop 报告成功但端口探测仍为真，断言启动器在 spawn 前失败关闭
def test_explicit_env_refuses_core_that_survives_required_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(Path.cwd()), "active_runs": 0},
    )
    monkeypatch.setattr(core, "_running_pid", lambda: 1234)
    monkeypatch.setattr(core, "stop_core", lambda _config: True)

    async def port_still_open(_config: CodeRookConfig) -> bool:
        return True

    spawn = MagicMock(side_effect=AssertionError("must not reuse or spawn on occupied port"))
    monkeypatch.setattr(core, "_port_open", port_still_open)
    monkeypatch.setattr(core, "_spawn_core", spawn)

    with pytest.raises(core.CoreLaunchError, match="refusing to reuse"):
        core.ensure_core_running(CodeRookConfig(), env_file=tmp_path / "deployment.env")

    spawn.assert_not_called()


# 功能：验证端口被未知 Core 占用时显式 env 文件不会等待并误复用该进程
# 设计：令认证元数据始终缺失但端口已开放，断言启动器在派生和轮询前失败关闭
def test_explicit_env_refuses_unverified_open_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(core, "_core_metadata", lambda _config: None)

    async def port_open(_config: CodeRookConfig) -> bool:
        return True

    monkeypatch.setattr(core, "_port_open", port_open)
    spawn = MagicMock(side_effect=AssertionError("must not spawn on occupied port"))
    monkeypatch.setattr(core, "_spawn_core", spawn)

    with pytest.raises(core.CoreLaunchError, match="unverified Core"):
        core.ensure_core_running(CodeRookConfig(), env_file=tmp_path / "deployment.env")

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


# 功能：验证空闲 daemon 绑定其他 workspace 时启动器在同一进程内切换到当前目录
# 设计：替换项目激活调用并让停止与派生在误调用时失败，证明普通切换不会中断其他前端连接
def test_ensure_core_running_switches_idle_managed_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(tmp_path / "other"), "active_runs": 0},
    )
    activated: list[Path] = []

    async def activate(_config: CodeRookConfig, workspace: Path) -> None:
        activated.append(workspace)

    monkeypatch.setattr(core, "_activate_workspace", activate)
    monkeypatch.setattr(
        core,
        "stop_core",
        MagicMock(side_effect=AssertionError("must not stop")),
    )
    monkeypatch.setattr(
        core,
        "_spawn_core",
        MagicMock(side_effect=AssertionError("must not spawn")),
    )

    assert core.ensure_core_running(CodeRookConfig(), timeout_s=1.0) is False
    assert activated == [current.resolve()]


# 功能：验证从受保护安装目录执行管理命令时复用 Core 当前工作区
# 设计：显式启用 reuse_existing 并让切换、停止与派生全部在误调用时失败，覆盖只读管理入口不扰动产品会话
def test_ensure_core_running_can_reuse_active_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = tmp_path / "active"
    active.mkdir()
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(active), "active_runs": 1},
    )
    for name in ("_activate_workspace", "stop_core", "_spawn_core"):
        monkeypatch.setattr(
            core,
            name,
            MagicMock(side_effect=AssertionError(f"must not call {name}")),
        )

    assert core.ensure_core_running(CodeRookConfig(), reuse_existing=True) is False
    assert Path.cwd() == active


# 功能：验证欢迎页不会复用仍绑定 CodeRook 源码的旧 Core
# 设计：模拟空闲旧 daemon 和一次成功重启，确认新进程继承欢迎目录而不是重新暴露内部源码
def test_ensure_core_running_replaces_protected_source_for_welcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    monkeypatch.chdir(welcome)
    metadata = iter(
        [
            {"workspace": str(source), "active_runs": 0},
            {"workspace": str(welcome), "active_runs": 0},
        ]
    )
    monkeypatch.setattr(core, "_core_metadata", lambda _config: next(metadata))
    registry = MagicMock()
    registry.is_welcome_workspace.side_effect = lambda path: Path(path) == welcome
    registry.is_protected_workspace.side_effect = lambda path: Path(path) in {source, welcome}
    monkeypatch.setattr(core, "ProjectRegistry", lambda: registry)
    stop = MagicMock(return_value=True)
    monkeypatch.setattr(core, "stop_core", stop)

    async def port_closed(_config: CodeRookConfig) -> bool:
        return False

    proc = MagicMock()
    proc.poll.return_value = None
    spawn = MagicMock(return_value=proc)
    monkeypatch.setattr(core, "_port_open", port_closed)
    monkeypatch.setattr(core, "_spawn_core", spawn)
    monkeypatch.setattr(core.time, "sleep", lambda _seconds: None)

    assert core.ensure_core_running(CodeRookConfig(), reuse_existing=True) is True
    stop.assert_called_once()
    spawn.assert_called_once_with()
    assert Path.cwd() == welcome


# 功能：验证旧 Core 在内部源码执行任务时不会被欢迎页静默中断或复用
# 设计：返回 active_runs=1 并监控停止调用，确认启动器明确失败且内部源码不会进入产品文件树
def test_ensure_core_running_refuses_busy_protected_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    monkeypatch.chdir(welcome)
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(source), "active_runs": 1},
    )
    registry = MagicMock()
    registry.is_welcome_workspace.side_effect = lambda path: Path(path) == welcome
    registry.is_protected_workspace.side_effect = lambda path: Path(path) in {source, welcome}
    monkeypatch.setattr(core, "ProjectRegistry", lambda: registry)
    stop = MagicMock(side_effect=AssertionError("must not stop active work"))
    monkeypatch.setattr(core, "stop_core", stop)

    with pytest.raises(core.CoreLaunchError, match="busy in CodeRook's internal source"):
        core.ensure_core_running(CodeRookConfig(), reuse_existing=True)

    stop.assert_not_called()


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


# 功能：验证手动 Core 模式无法证明同一 env overlay 时必须失败关闭
# 设计：即使 Core 正确绑定当前 workspace，也传入 env 路径并断言给出可恢复提示
def test_validate_core_workspace_rejects_unverifiable_explicit_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(Path.cwd()), "active_runs": 0},
    )

    with pytest.raises(core.CoreLaunchError, match="Cannot verify"):
        core.validate_core_workspace(
            CodeRookConfig(),
            env_file=tmp_path / "deployment.env",
        )


# 功能：验证从其他目录重启 Core 时仍保留 daemon 当前服务的项目
# 设计：用当前元数据指向独立项目，在停止与再启动之间捕获 cwd 排除回落到调用目录
def test_core_restart_preserves_active_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    caller = tmp_path / "caller"
    active = tmp_path / "active"
    caller.mkdir()
    active.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(active), "active_runs": 0},
    )
    monkeypatch.setattr(core, "stop_core", lambda _config: True)
    observed: list[Path] = []

    def ensure(_config: CodeRookConfig, *, env_file: Path | None = None) -> bool:
        del env_file
        observed.append(Path.cwd())
        return True

    monkeypatch.setattr(core, "ensure_core_running", ensure)
    monkeypatch.setattr(core, "_running_pid", lambda: 1234)

    core.cmd_core_restart(CodeRookConfig())

    assert observed == [active.resolve()]


# 功能：验证重启旧源码 Core 时自动迁移到隔离欢迎目录
# 设计：让项目注册表明确标记源码与欢迎目录，直接检查重启前采用的工作目录
def test_core_restart_does_not_adopt_protected_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    monkeypatch.setattr(
        core,
        "_core_metadata",
        lambda _config: {"workspace": str(source), "active_runs": 0},
    )
    registry = MagicMock()
    registry.is_protected_workspace.side_effect = lambda path: Path(path) == source
    registry.is_welcome_workspace.side_effect = lambda path: Path(path) == welcome
    registry.prepare_welcome_workspace.return_value = welcome
    monkeypatch.setattr(core, "ProjectRegistry", lambda: registry)

    adopted = core._adopt_restart_workspace(CodeRookConfig())

    assert adopted == welcome
    assert Path.cwd() == welcome

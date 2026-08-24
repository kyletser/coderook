from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_rook.core.config import CodeRookConfig, LlmConfig
from code_rook.tui import __main__ as tui_main
from code_rook.tui.app import ConfigSwitch, ModelSwitch


# 功能：默认启动 TUI 时先确保 Core 就绪，再读取 token 并运行界面
# 设计：替换所有外部边界并记录调用顺序，避免测试启动真实 daemon 或 Textual 终端
def test_tui_main_auto_starts_core_before_reading_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(sys, "argv", ["coderook-tui"])
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(
        tui_main,
        "ensure_core_running",
        lambda _config: calls.append("core"),
    )
    monkeypatch.setattr(
        tui_main,
        "read_ipc_token",
        lambda _path: calls.append("token") or "x" * 32,
    )
    app = MagicMock()
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", MagicMock(return_value=app))

    tui_main.main()

    assert calls == ["core", "token"]
    assert callable(tui_main.CodeRookTuiApp.call_args.kwargs["core_recovery"])
    assert tui_main.CodeRookTuiApp.call_args.kwargs["continue_recent"] is True
    app.run.assert_called_once_with()


# 功能：验证 TUI 显式环境文件同时用于本进程配置和自动启动的 Core
# 设计：捕获配置加载与 daemon 启动参数，确保两进程读取同一用户选择且不依赖仓库 .env
def test_tui_main_forwards_explicit_env_file_to_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        "CODEROOK_PORT=7666\nDEPLOYMENT_LLM_KEY=explicit-file-secret\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEPLOYMENT_LLM_KEY", raising=False)
    config = CodeRookConfig(
        ipc_token_file=str(tmp_path / "ipc-token"),
        llm=LlmConfig(
            credential_overlay={"DEPLOYMENT_LLM_KEY": "explicit-file-secret"}
        ),
    )
    loaded: list[Path] = []
    launched: list[Path] = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook-tui", "--env-file", str(env_file)],
    )
    monkeypatch.setattr(
        tui_main,
        "get_config",
        lambda *, env_file: loaded.append(env_file) or config,
    )
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(
        tui_main,
        "ensure_core_running",
        lambda _config, *, env_file: launched.append(env_file) or False,
    )
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", MagicMock(return_value=app))

    tui_main.main()

    assert loaded == [env_file]
    assert launched == [env_file]
    credentials = tui_main.CodeRookTuiApp.call_args.kwargs["credential_store"]
    assert credentials.resolve("env:DEPLOYMENT_LLM_KEY").value == "explicit-file-secret"
    assert "explicit-file-secret" not in repr(config)
    assert "explicit-file-secret" not in repr(tui_main.CodeRookTuiApp.call_args.kwargs)
    app.run.assert_called_once_with()


# 功能：--no-auto-core 保留手动管理模式并校验 Core 绑定当前 workspace
# 设计：使用有效测试 token 和 fake app，断言不调用自动启动器但必须执行只读 workspace 校验
def test_tui_main_can_disable_auto_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(sys, "argv", ["coderook-tui", "--no-auto-core"])
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    ensure = MagicMock()
    monkeypatch.setattr(tui_main, "ensure_core_running", ensure)
    validate = MagicMock()
    monkeypatch.setattr(tui_main, "validate_core_workspace", validate)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", MagicMock(return_value=app))

    tui_main.main()

    ensure.assert_not_called()
    validate.assert_called_once_with(config, env_file=None)
    assert tui_main.CodeRookTuiApp.call_args.kwargs["core_recovery"] is None
    app.run.assert_called_once_with()


# 功能：验证 --continue 会传入 TUI，使连接层恢复当前 workspace 最近会话
# 设计：隔离 daemon、token 和 Textual 边界，检查构造参数而不依赖真实持久 session
def test_tui_main_passes_continue_recent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(sys, "argv", ["coderook-tui", "--continue"])
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    assert factory.call_args.kwargs["continue_recent"] is True
    app.run.assert_called_once_with()


# 功能：验证 --new 是裸启动自动恢复的显式退出开关
# 设计：隔离 daemon、token 和 Textual 边界，断言参数只改变会话选择策略
def test_tui_main_new_forces_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(sys, "argv", ["coderook-tui", "--new"])
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    assert factory.call_args.kwargs["continue_recent"] is False
    app.run.assert_called_once_with()


# 功能：验证首次 TUI 启动不再把默认模板伪装为已配置 route
# 设计：检查入口删除隐式迁移函数，使空 RouteStore 能由 readiness 真实呈现为未配置
def test_tui_does_not_materialize_legacy_route_on_startup() -> None:
    assert not hasattr(tui_main, "_ensure_route_configured")


# 功能：验证空 route 启动参数不会回退到内置 provider/model 模板并伪装为可执行
# 设计：替换 RouteStore 为真实空视图并检查 App 构造参数，禁止调用旧 model catalog 回退
def test_run_tui_passes_empty_model_state_without_active_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _EmptyRoutes:
        # 返回没有活动 route 的只读视图
        def active(self) -> None:
            return None

    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    args = SimpleNamespace(
        no_auto_core=False,
        replay=None,
        resume=None,
        continue_recent=True,
    )
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: _EmptyRoutes())
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    catalog = MagicMock(side_effect=AssertionError("model catalog fallback forbidden"))
    monkeypatch.setattr(tui_main, "list_models", catalog)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main._run_tui(args)

    kwargs = factory.call_args.kwargs
    assert kwargs["provider"] == ""
    assert kwargs["model"] == ""
    assert kwargs["models"] == []
    assert kwargs["route"] == ""
    catalog.assert_not_called()


# 功能：验证模型切换会保存目录和默认模型、重启 Core 并恢复当前会话
# 设计：用两次 TUI 返回值驱动入口循环，记录边界调用参数而不启动真实进程
def test_tui_main_switches_model_and_resumes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_rook.cli.commands import core as core_commands

    config = CodeRookConfig()
    actions = iter([ModelSwitch("claude-opus-4-6", "session-1"), None])
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "argv", ["coderook-tui"])
    monkeypatch.setattr(tui_main, "_run_tui", lambda args: actions.__next__())
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(
        tui_main,
        "add_model",
        lambda provider, model: calls.append((provider, model)),
    )
    monkeypatch.setattr(
        tui_main,
        "switch_llm_model",
        lambda _config, model: calls.append(("switch", model)),
    )
    monkeypatch.setattr(
        core_commands,
        "stop_core",
        lambda *_args: calls.append(("core", "stop")),
    )

    tui_main.main()

    assert calls == [
        ("anthropic", "claude-opus-4-6"),
        ("switch", "claude-opus-4-6"),
        ("core", "stop"),
    ]


# 功能：验证内联配置结果会保存全部已探测模型、Provider 配置并恢复会话
# 设计：构造 ConfigSwitch 驱动入口循环，检查持久化顺序且不启动真实 Core
def test_tui_main_saves_discovered_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from code_rook.cli.commands import core as core_commands

    config = CodeRookConfig()
    action = ConfigSwitch(
        provider="deepseek",
        api_key="api-test",
        model="deepseek-v4-pro",
        models=("deepseek-v4-pro", "deepseek-v4-flash"),
        session_id="session-2",
    )
    actions = iter([action, None])
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(sys, "argv", ["coderook-tui"])
    monkeypatch.setattr(tui_main, "_run_tui", lambda args: actions.__next__())
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(
        tui_main,
        "add_models",
        lambda provider, models: calls.append(("models", provider, *models)),
    )
    monkeypatch.setattr(
        tui_main,
        "save_provider_config",
        lambda _current, provider, _key, model: calls.append(("config", provider, model)),
    )
    monkeypatch.setattr(
        core_commands,
        "stop_core",
        lambda *_args: calls.append(("core", "stop")),
    )

    tui_main.main()

    assert calls == [
        ("models", "deepseek", "deepseek-v4-pro", "deepseek-v4-flash"),
        ("config", "deepseek", "deepseek-v4-pro"),
        ("core", "stop"),
    ]

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.route_store import RouteStore
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
    monkeypatch.setattr(tui_main, "_ensure_llm_configured", lambda: None)
    monkeypatch.setattr(tui_main, "_ensure_route_configured", lambda _config: None)
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
    app.run.assert_called_once_with()


# 功能：--no-auto-core 保留手动管理模式且不会调用自动启动器
# 设计：使用有效测试 token 和 fake app，断言启动器为零调用但 TUI 仍正常运行
def test_tui_main_can_disable_auto_core(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(sys, "argv", ["coderook-tui", "--no-auto-core"])
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "_ensure_llm_configured", lambda: None)
    monkeypatch.setattr(tui_main, "_ensure_route_configured", lambda _config: None)
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    ensure = MagicMock()
    monkeypatch.setattr(tui_main, "ensure_core_running", ensure)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", MagicMock(return_value=app))

    tui_main.main()

    ensure.assert_not_called()
    app.run.assert_called_once_with()


# 功能：首次启动缺少 LLM 配置时在启动 Core 之前执行交互式向导
# 设计：模拟 TTY 和未配置状态，记录 configure_llm 调用，确保不会先启动一个必然因缺 key 退出的 daemon
def test_first_tui_start_runs_llm_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CodeRookConfig()
    calls: list[str] = []
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "llm_is_configured", lambda _config: False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        tui_main,
        "configure_llm",
        lambda _config: calls.append("configure") or config,
    )

    tui_main._ensure_llm_configured()

    assert calls == ["configure"]


# 功能：验证首次 TUI 启动把旧 LLM 配置迁移为显式活动 route
# 设计：使用临时 RouteStore 连续调用两次，断言迁移幂等且模型和 wire 不靠前缀推断
def test_tui_migrates_legacy_config_to_route(tmp_path: Path) -> None:
    routes = RouteStore(tmp_path / "routes.json")
    config = CodeRookConfig()

    tui_main._ensure_route_configured(config, routes)
    tui_main._ensure_route_configured(config, routes)

    assert len(routes.list()) == 1
    active = routes.active()
    assert active is not None
    assert active.model == config.llm.default_model
    assert active.wire_format == "anthropic_messages"


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
    monkeypatch.setattr(tui_main, "_ensure_llm_configured", lambda: None)
    monkeypatch.setattr(tui_main, "_ensure_route_configured", lambda _config: None)
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
    monkeypatch.setattr(tui_main, "_ensure_llm_configured", lambda: None)
    monkeypatch.setattr(tui_main, "_ensure_route_configured", lambda _config: None)
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
        lambda _current, provider, _key, model: calls.append(
            ("config", provider, model)
        ),
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

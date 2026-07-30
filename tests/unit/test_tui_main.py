from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_rook.core.config import CodeRookConfig
from code_rook.tui import __main__ as tui_main


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

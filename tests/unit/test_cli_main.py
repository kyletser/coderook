from __future__ import annotations

import sys

from code_rook.cli import main as cli_main
from code_rook.core.config import CodeRookConfig
from code_rook.tui import __main__ as tui_main


# 功能：验证无参数 coderook 直接进入 TUI 启动路径
# 设计：替换 TUI 入口并固定 argv，确认 CLI 只委托一次且不重复执行旧状态迁移
def test_no_arguments_launches_tui(
    monkeypatch,
) -> None:
    launched: list[bool] = []
    migrated: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["coderook"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(True))
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: migrated.append(True))

    cli_main.main()

    assert launched == [True]
    assert migrated == []


# 功能：验证 coderook --continue 直接委托 TUI 的最近会话恢复入口
# 设计：保留原始 argv 并替换 TUI main，确认该顶层体验参数不会落入旧 CLI argparse
def test_continue_flag_launches_tui(monkeypatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["coderook", "--continue"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(list(sys.argv)))

    cli_main.main()

    assert launched == [["coderook", "--continue"]]


# 功能：验证带参数的 coderook 仍由原 CLI 分发器处理
# 设计：使用无配置依赖的 --version 路径，断言旧迁移和版本命令各执行一次且不会启动 TUI
def test_explicit_arguments_keep_cli_dispatch(
    monkeypatch,
) -> None:
    launched: list[bool] = []
    migrated: list[bool] = []
    versioned: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["coderook", "--version"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(True))
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: migrated.append(True))
    monkeypatch.setattr(cli_main, "cmd_version", lambda: versioned.append(True))

    cli_main.main()

    assert launched == []
    assert migrated == [True]
    assert versioned == [True]


# 功能：验证 coderook review 参数被分发到只读审查 preset
# 设计：替换配置、日志和命令执行入口，仅验证 argparse 到业务参数的公开 CLI 契约
def test_review_command_dispatches_structured_preset(monkeypatch) -> None:
    captured: dict[str, object] = {}
    config = CodeRookConfig()
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "review", "--goal", "Review auth", "--output-format", "json"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_review",
        lambda goal, passed_config, **kwargs: captured.update(
            {"goal": goal, "config": passed_config, **kwargs}
        ),
    )

    cli_main.main()

    assert captured == {
        "goal": "Review auth",
        "config": config,
        "output_format": "json",
    }

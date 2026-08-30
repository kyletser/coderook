from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_rook.cli import main as cli_main
from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.tui import __main__ as tui_main


# 功能：验证 CLI 在 Windows 管道场景主动把 stdout 和 stderr 切换为 UTF-8
# 设计：用记录 reconfigure 参数的最小流替换系统流，直接验证两个输出通道采用同一稳定编码
def test_cli_configures_utf8_stdio(monkeypatch) -> None:
    class _Stream:
        # 初始化标准流重配置调用记录
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        # 记录 CLI 请求的编码和错误策略
        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.calls.append((encoding, errors))

    stdout = _Stream()
    stderr = _Stream()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    cli_main._configure_utf8_stdio()

    assert stdout.calls == [("utf-8", "replace")]
    assert stderr.calls == [("utf-8", "replace")]


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


# 功能：验证 coderook --new 直接委托 TUI 的全新会话入口
# 设计：保留顶层 argv 并替换 TUI main，防止该参数误入脚本 CLI 的 argparse
def test_new_flag_launches_tui(monkeypatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["coderook", "--new"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(list(sys.argv)))

    cli_main.main()

    assert launched == [["coderook", "--new"]]


# 功能：验证显式 coderook tui 别名移除子命令后完整委托原 TUI 参数解析器
# 设计：捕获 TUI 看到的 argv，确保显式别名不改变 --continue 等既有启动语义
def test_explicit_tui_alias_launches_tui(monkeypatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["coderook", "tui", "--continue"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(list(sys.argv)))

    cli_main.main()

    assert launched == [["coderook", "--continue"]]


# 功能：验证 coderook web 可切换到显式工作区并把 no-open 选项交给 Web 启动器
# 设计：替换配置与启动器并记录 cwd，覆盖 argparse、路径解析和 Core 启动前工作区绑定
def test_web_command_dispatches_selected_workspace(monkeypatch, tmp_path: Path) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    original = Path.cwd()
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "web", str(tmp_path), "--no-open"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_web",
        lambda passed, **kwargs: captured.update(
            {"config": passed, "cwd": Path.cwd(), **kwargs}
        )
        or 0,
    )
    try:
        result = cli_main.main()
    finally:
        os.chdir(original)

    assert result == 0
    assert captured == {
        "config": config,
        "cwd": tmp_path.resolve(),
        "no_open": True,
        "env_file": None,
    }


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


# 功能：验证脚本 CLI 只在显式 --env-file 时把环境文件交给安全配置加载器
# 设计：选择无网络的 config-status 路径并捕获关键字参数，证明仓库 .env 不会因 cwd 自动注入
def test_explicit_env_file_is_forwarded_to_config_loader(
    monkeypatch,
    tmp_path,
) -> None:
    env_file = tmp_path / "deployment.env"
    env_file.write_text("CODEROOK_PORT=7666\n", encoding="utf-8")
    config = CodeRookConfig()
    loaded: list[Path] = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "--env-file", str(env_file), "config-status"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(
        cli_main,
        "get_config",
        lambda *, env_file: loaded.append(env_file) or config,
    )
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "print_llm_status", lambda _config: None)

    cli_main.main()

    assert loaded == [env_file]


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


# 功能：验证 memory auto 子命令分发为 typed 设置参数
# 设计：替换 daemon 命令函数并固定 argv，只检查 argparse 到治理入口的稳定契约
def test_memory_auto_command_dispatches_typed_setting(monkeypatch) -> None:
    captured: dict[str, object] = {}
    config = CodeRookConfig()
    monkeypatch.setattr(sys, "argv", ["coderook", "memory", "auto", "off"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_memory",
        lambda passed_config, action, **kwargs: captured.update(
            {"config": passed_config, "action": action, **kwargs}
        )
        or 0,
    )

    result = cli_main.main()

    assert result == 0
    assert captured == {
        "config": config,
        "action": "auto",
        "params": {"auto_save": "off"},
    }


# 功能：验证 runtime Doctor 的不健康退出码会穿过 CLI 分发器返回给脚本
# 设计：替换诊断实现为固定失败码并同时传入 JSON/repair，排除文本格式分支吞掉状态
def test_runtime_doctor_exit_code_is_preserved(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, bool] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "doctor", "runtime", "--repair", "--json"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_runtime_doctor",
        lambda *, repair, as_json: captured.update(
            {"repair": repair, "as_json": as_json}
        )
        or 1,
    )

    result = cli_main.main()

    assert result == 1
    assert captured == {"repair": True, "as_json": True}


# 功能：验证 CLI 边界把损坏凭据文档转成脱敏非零结果而不泄露原始正文
# 设计：让真实 CredentialStore 在 provider list 分支读取坏文件，断言返回码与 stderr 均稳定
def test_cli_credential_store_error_is_safe_and_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_path = tmp_path / "credentials.json"
    credential_path.write_text('{"api_keys":{"secret":"do-not-print"}', encoding="utf-8")
    config = CodeRookConfig()
    monkeypatch.setattr(sys, "argv", ["coderook", "provider", "list"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)

    # 让分发目标触发真实 typed 凭据读取故障
    def read_corrupt_store(_config: CodeRookConfig) -> None:
        CredentialStore(credential_path).resolve("file:route-a")

    monkeypatch.setattr(cli_main, "cmd_provider_list", read_corrupt_store)

    result = cli_main.main()
    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert captured.err == "credential store error: credential store contains invalid JSON\n"
    assert "do-not-print" not in captured.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["doctor", "runtime"],
        ["doctor", "runtime", "--json"],
        ["doctor", "runtime", "--repair", "--json"],
    ],
)
# 功能：验证 python -m CLI 在三种 Runtime Doctor 失败输出下都返回非零
# 设计：用损坏迁移标记构造真实不健康状态并启动子进程，覆盖模块入口的 SystemExit 传播
def test_module_runtime_doctor_returns_nonzero_when_unhealthy(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    state_root = tmp_path / ".coderook"
    marker_root = state_root / "migrations"
    marker_root.mkdir(parents=True)
    (marker_root / "provider-catalog-v1.json").write_text("{}", encoding="utf-8")
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path)
    environment["USERPROFILE"] = str(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "code_rook.cli", *arguments],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1
    assert "healthy" in result.stdout or "provider_catalog_migration_backup" in result.stdout


# 功能：验证项目旧状态只有显式 migrate-project-state --yes 才请求复制
# 设计：替换迁移函数并让配置加载在误调用时失败，证明命令在读取项目配置前完成确认
def test_migrate_project_state_requires_explicit_command(
    monkeypatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        cli_main,
        "migrate_legacy_state",
        lambda *, include_project=False: calls.append(include_project)
        or type(
            "Report",
            (),
            {"legacy_project_state_found": True, "project_files_copied": 2},
        )(),
    )
    monkeypatch.setattr(
        cli_main,
        "get_config",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not load config")),
    )
    monkeypatch.setattr(sys, "argv", ["coderook", "migrate-project-state", "--yes"])

    result = cli_main.main()

    assert result == 0
    assert calls == [False, True]

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_rook.cli import main as cli_main
from code_rook.cli.commands.run import _event_belongs_to_run
from code_rook.cli.commands.sessions import (
    _display_timestamp,
    _display_title,
    _visible_sessions,
)
from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.credentials import CredentialStore
from code_rook.tui import __main__ as tui_main


# 保持 CLI 单元测试所在目录不被产品入口切换，项目重定向行为由专项测试覆盖
@pytest.fixture(autouse=True)
def _keep_cli_test_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli_main.ProjectRegistry,
        "enter_welcome_workspace_if_protected",
        lambda _self: Path.cwd(),
    )
    monkeypatch.setattr(cli_main, "_stdio_supports_tui", lambda: True)


# 功能：验证 headless run 只接收自身事件，同时允许无归属的全局状态事件
# 设计：直接覆盖匹配、其他 run 和缺失 run_id 三种输入，锁定早期缓冲回放的过滤规则
def test_headless_run_event_ownership_filter() -> None:
    assert _event_belongs_to_run({"type": "run.started", "run_id": "run-1"}, "run-1")
    assert not _event_belongs_to_run(
        {"type": "tool.call_started", "run_id": "run-2"}, "run-1"
    )
    assert _event_belongs_to_run({"type": "core.status"}, "run-1")


# 功能：验证命令行内联 @引用把普通文件注入文本、把图片分离为多模态附件。
# 设计：同时引用大小写不同的图片后缀与文本文件，锁定两类输入不会重复或混淆。
def test_prepare_file_referenced_input_separates_images(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("REFERENCE_TEXT", encoding="utf-8")
    (tmp_path / "screen.PNG").write_bytes(b"image-placeholder")

    content, images = cli_main._prepare_file_referenced_input(
        "比较 @notes.txt 和 @screen.PNG",
        tmp_path,
    )

    assert "REFERENCE_TEXT" in content
    assert '<file path="screen.PNG"' not in content
    assert images == [tmp_path / "screen.PNG"]


# 功能：命令行允许通过含空格的 Windows 绝对路径引用当前工作区内图片
# 设计：把含空格绝对路径同时放入正文和 standalone 候选，复现 shell 引号消失后的真实 argv 输入
def test_prepare_file_reference_accepts_windows_absolute_path(tmp_path: Path) -> None:
    folder = tmp_path / "folder with space"
    folder.mkdir()
    image = folder / "screen image.png"
    image.write_bytes(b"image-placeholder")

    content, images = cli_main._prepare_file_referenced_input(
        f"@{image} describe",
        tmp_path,
        standalone_references=[str(image)],
    )

    assert content == f"@{image} describe"
    assert images == [image]


# 功能：命令行绝对图片路径可由用户显式选择而不要求位于当前项目
# 设计：将项目与图片放在同级目录，确认解析返回外部原路径且不扩大模型工具工作区
def test_prepare_file_reference_accepts_explicit_external_image(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    image = tmp_path / "outside image.png"
    image.write_bytes(b"image-placeholder")

    content, images = cli_main._prepare_file_referenced_input(
        f"@{image} describe",
        workspace,
        standalone_references=[str(image)],
    )

    assert content == f"@{image} describe"
    assert images == [image]


# 功能：引号包住整条任务时，CLI 只把 @文件片段识别为引用而不吞掉后续指令。
# 设计：模拟 PowerShell 将整句作为单个 argv 传入，同时保留对应 standalone 候选。
def test_prepare_file_reference_does_not_treat_quoted_task_as_path(
    tmp_path: Path,
) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(b"image-placeholder")

    content, images = cli_main._prepare_file_referenced_input(
        "@screen.png 请描述图片",
        tmp_path,
        standalone_references=["screen.png 请描述图片"],
    )

    assert content == "@screen.png 请描述图片"
    assert images == [image]


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
# 设计：替换 TUI 入口并让顶层工作区重定向在误调用时失败，确认重定向只由 TUI 自身执行一次
def test_no_arguments_launches_tui(
    monkeypatch,
) -> None:
    launched: list[bool] = []
    migrated: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["coderook"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(True))
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: migrated.append(True))
    monkeypatch.setattr(
        cli_main.ProjectRegistry,
        "enter_welcome_workspace_if_protected",
        MagicMock(side_effect=AssertionError("TUI owns workspace redirection")),
    )

    cli_main.main()

    assert launched == [True]
    assert migrated == []


# 功能：顶层帮助直接展示交互、单次打印、Web 和脚本四个主要产品入口
# 设计：执行真实 argparse 帮助并捕获退出，避免帮助文本再次与已实现的快捷路由漂移
def test_top_level_help_exposes_primary_entrypoints(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["coderook", "--help"])

    with pytest.raises(SystemExit) as raised:
        cli_main.main()

    output = capsys.readouterr().out
    assert raised.value.code == 0
    assert 'coderook "fix the failing tests"' in output
    assert "coderook tui" in output
    assert "coderook -p" in output
    assert "coderook web" in output
    assert "coderook run --help" in output
    assert "--route" in output


# 功能：会话列表帮助用紧凑范围占位符展示 limit 而不是展开两百个数字
# 设计：执行真实子命令帮助并检查公开文本，同时锁定越界参数返回 argparse 错误
def test_sessions_limit_help_is_compact_and_range_checked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["coderook", "sessions", "--help"])
    with pytest.raises(SystemExit) as raised:
        cli_main.main()
    output = capsys.readouterr().out
    assert raised.value.code == 0
    assert "--limit 1..200" in output
    assert "{1,2,3,4" not in output

    assert cli_main._session_list_limit("1") == 1
    assert cli_main._session_list_limit("200") == 200
    with pytest.raises(argparse.ArgumentTypeError):
        cli_main._session_list_limit("201")


# 功能：验证默认 CLI 会话列表隐藏无标题零运行临时项而 --all 保留完整记录
# 设计：用三类最小投影直接检查过滤结果，覆盖有效会话、用户命名空会话和中断残留空会话
def test_session_list_hides_only_unused_untitled_sessions() -> None:
    sessions = [
        {"session_id": "used", "run_count": 1, "title": ""},
        {"session_id": "named", "run_count": 0, "title": "draft"},
        {"session_id": "empty", "run_count": 0, "title": ""},
    ]

    visible = _visible_sessions(sessions, include_empty=False)
    assert [item["session_id"] for item in visible] == ["used", "named"]
    assert _visible_sessions(sessions, include_empty=True) == sessions


# 功能：验证 CLI 会话标题折叠换行并限制为单行摘要
# 设计：同时覆盖空标题、多段空白和超长文本，防止真实提示词破坏终端表格布局
def test_session_list_normalizes_title_for_terminal_table() -> None:
    assert _display_title("") == "(untitled)"
    assert _display_title("first\n\nsecond\tpart") == "first second part"
    assert _display_title("abcdefgh", limit=6) == "abcde…"


# 功能：验证会话列表把 Runtime UTC 时间转换为用户所在时区
# 设计：注入固定东八区避免依赖 CI 主机设置，同时覆盖 Z、无时区旧值和损坏值
def test_session_timestamp_display_uses_local_timezone() -> None:
    local_timezone = timezone(timedelta(hours=8))

    assert (
        _display_timestamp("2026-09-10T15:14:04Z", local_timezone=local_timezone)
        == "2026-09-10 23:14:04"
    )
    assert (
        _display_timestamp("2026-09-10T15:14:04", local_timezone=local_timezone)
        == "2026-09-10 23:14:04"
    )
    assert _display_timestamp("legacy-time", local_timezone=local_timezone) == "legacy-time"


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


# 功能：顶层位置参数按 Pi 风格作为初始任务交给 TUI，而不是被 argparse 当成未知子命令。
# 设计：捕获委托后的原始参数，确认中文多段任务不经过脚本 run 子命令重写。
def test_positional_prompt_launches_tui(monkeypatch) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", ["coderook", "修复登录", "并运行测试"])
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(list(sys.argv)))

    cli_main.main()

    assert launched == [["coderook", "修复登录", "并运行测试"]]


# 功能：验证非交互终端中的裸任务自动使用一次性文本执行而不启动全屏 TUI
# 设计：固定 stdio 为非 TTY 并替换 Core 边界，直接锁定 IDE、管道和子进程调用能执行后退出
def test_positional_prompt_uses_print_mode_without_tty(monkeypatch) -> None:
    config = CodeRookConfig()
    launched: list[bool] = []
    captured: dict[str, object] = {}
    monkeypatch.setattr(sys, "argv", ["coderook", "只回答", "NON_TTY_OK"])
    monkeypatch.setattr(cli_main, "_stdio_supports_tui", lambda: False)
    monkeypatch.setattr(tui_main, "main", lambda: launched.append(True))
    monkeypatch.setattr(cli_main, "_read_piped_stdin", lambda: "")
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert launched == []
    assert captured["goal"] == "只回答 NON_TTY_OK"
    assert captured["output_format"] == "text"
    assert captured["final_only"] is True


# 功能：-p 以一次性 Python Agent 模式自动启动 Core 并打印任务结果。
# 设计：替换 daemon 与 headless 边界，核对任务拼接和四个 Pi 原生工具的显式允许列表。
def test_print_shorthand_runs_native_headless_agent(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coderook",
            "-p",
            "--thinking",
            "high",
            "--system-prompt",
            "You are a release reviewer.",
            "--append-system-prompt",
            "Only report verified facts.",
            "读取项目",
            "总结结构",
        ],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    ensure = MagicMock()
    monkeypatch.setattr(cli_main, "ensure_core_running", ensure)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, passed, **kwargs: captured.update(
            {"goal": goal, "config": passed, **kwargs}
        ),
    )

    result = cli_main.main()

    assert result == 0
    ensure.assert_called_once_with(config, env_file=None)
    assert captured == {
        "goal": "读取项目 总结结构",
        "config": config,
        "display_content": "读取项目 总结结构",
        "permission_mode": "allow_list",
        "allow_tools": ["read", "bash", "edit", "write"],
        "model_tools": None,
        "output_format": "text",
        "final_only": True,
        "image_paths": [],
        "session_mode": "chat",
        "session_name": "",
        "delete_session_after": False,
        "resume_session_id": None,
        "fork_session_id": None,
        "continue_recent": False,
        "route_id": None,
        "model": None,
        "thinking_level": "high",
        "system_prompt": "You are a release reviewer.",
        "append_system_prompt": "Only report verified facts.",
    }


# 功能：常规 run 命令支持为单次自动化任务替换并追加系统指令
# 设计：捕获 CLI 到 cmd_run 的参数，证明两个提示参数不会混入用户目标或只在快捷入口生效
def test_run_command_forwards_task_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coderook",
            "run",
            "--goal",
            "检查发布状态",
            "--system-prompt",
            "You are a release reviewer.",
            "--append-system-prompt",
            "Only report verified facts.",
        ],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["goal"] == "检查发布状态"
    assert captured["system_prompt"] == "You are a release reviewer."
    assert captured["append_system_prompt"] == "Only report verified facts."


# 功能：快捷打印模式支持继续当前工作区最近的非空会话
# 设计：捕获 cmd_run 参数，验证 --continue 与 -p 可组合且不会被快捷解析器拒绝
def test_print_shorthand_can_continue_recent_session(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "-c", "-p", "总结上一轮结果"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["goal"] == "总结上一轮结果"
    assert captured["session_mode"] == "chat"
    assert captured["delete_session_after"] is False
    assert captured["continue_recent"] is True
    assert captured["resume_session_id"] is None


# 功能：快捷打印入口可从指定历史会话创建新分支并在分支中执行任务。
# 设计：捕获 --fork 的 headless 参数，确认它不会被误解释为原会话 resume 或最近会话 continue。
def test_print_shorthand_can_fork_session(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "-p", "--fork", "sess-source", "尝试另一种实现"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["fork_session_id"] == "sess-source"
    assert captured["resume_session_id"] is None
    assert captured["continue_recent"] is False


# 功能：快捷打印模式支持显式临时会话，不把一次性任务留在历史列表。
# 设计：捕获执行参数，确认临时模式仍使用一次性会话并在完成后请求删除。
def test_print_shorthand_supports_no_session(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "-p", "--no-session", "只回答 TEMP_OK"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["session_mode"] == "one_shot"
    assert captured["delete_session_after"] is True


# 功能：快捷打印模式可显式关闭全部模型工具，适合纯文本问答和低开销脚本。
# 设计：捕获 -nt 分发参数并与权限白名单分开断言，证明工具目录收窄不会改变审批策略。
def test_print_shorthand_supports_no_tools(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(sys, "argv", ["coderook", "-p", "-nt", "只回答 OK"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["model_tools"] == []
    assert captured["allow_tools"] == ["read", "bash", "edit", "write"]


# 功能：快捷打印入口可在首次运行时直接设置可辨识的会话名称。
# 设计：从公开 -n 参数捕获业务调用，确保名称不依赖运行结束后的二次重命名命令。
def test_print_shorthand_sets_session_name(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coderook", "-p", "-n", "认证修复",
            "--provider", "aliyun", "--model", "qwen3.8-flash",
            "检查登录逻辑",
        ],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["session_name"] == "认证修复"
    assert captured["goal"] == "检查登录逻辑"
    assert captured["route_id"] == "aliyun"
    assert captured["model"] == "qwen3.8-flash"


# 功能：从受保护源码目录执行打印任务时复用 Core 已绑定的用户项目
# 设计：让项目重定向真实改变 cwd，并断言启动器采用 reuse_existing，避免把欢迎区当成可注册项目
def test_print_from_protected_source_reuses_active_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    config = CodeRookConfig()
    ensure = MagicMock()
    original = Path.cwd()
    monkeypatch.setattr(sys, "argv", ["coderook", "-p", "--no-tools", "hello"])
    monkeypatch.setattr(
        cli_main.ProjectRegistry,
        "enter_welcome_workspace_if_protected",
        lambda _self: os.chdir(welcome) or welcome,
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", ensure)
    monkeypatch.setattr(cli_main, "cmd_run", lambda *_args, **_kwargs: None)
    try:
        os.chdir(source)
        assert cli_main.main() == 0
    finally:
        os.chdir(original)

    ensure.assert_called_once_with(config, env_file=None, reuse_existing=True)


# 功能：从受保护源码目录执行 run 子命令时同样复用当前用户项目
# 设计：走完整子命令解析并捕获 Core 启动参数，防止修复只覆盖快捷打印入口
def test_run_from_protected_source_reuses_active_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    config = CodeRookConfig()
    ensure = MagicMock()
    original = Path.cwd()
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "run", "--goal", "hello", "--no-tools"],
    )
    monkeypatch.setattr(
        cli_main.ProjectRegistry,
        "enter_welcome_workspace_if_protected",
        lambda _self: os.chdir(welcome) or welcome,
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", ensure)
    monkeypatch.setattr(cli_main, "cmd_run", lambda *_args, **_kwargs: None)
    try:
        os.chdir(source)
        assert cli_main.main() == 0
    finally:
        os.chdir(original)

    ensure.assert_called_once_with(config, env_file=None, reuse_existing=True)


# 功能：已配置的 route/model 简写会拆成独立 Route 与模型覆盖
# 设计：使用最小 RouteStore stub 同时覆盖命中分支，避免依赖用户真实 Provider 配置
def test_resolve_provider_prefixed_model_uses_matching_route() -> None:
    route = SimpleNamespace(id="aliyun")
    routes = SimpleNamespace(get=lambda route_id: route, active=lambda: route)

    resolved = cli_main._resolve_requested_route_and_model(
        None,
        "aliyun/qwen3.8-flash",
        routes,
    )

    assert resolved == ("aliyun", "qwen3.8-flash")


# 功能：模型自身带斜杠但前缀不是 Route 时仍由活动 Route 原样承载
# 设计：让 get 明确抛出 RouteStoreError，验证 OpenRouter 风格模型 ID 不被错误拆分
def test_resolve_slash_model_preserves_unknown_route_prefix() -> None:
    active = SimpleNamespace(id="openrouter")

    class _Routes:
        # 模拟没有与模型前缀同名的已配置 Route
        def get(self, _route_id: str) -> None:
            raise cli_main.RouteStoreError("missing")

        # 返回承载完整模型 ID 的活动 Route
        def active(self) -> SimpleNamespace:
            return active

    resolved = cli_main._resolve_requested_route_and_model(
        None,
        "anthropic/claude-sonnet",
        _Routes(),
    )

    assert resolved == ("openrouter", "anthropic/claude-sonnet")


# 功能：脚本 run 可选择逗号分隔的模型工具并在重复项中保持稳定顺序。
# 设计：通过公开 argparse 入口传入 read,bash,read，捕获业务调用确认去重后目录与权限参数独立。
def test_run_command_selects_model_tools(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "run", "--goal", "inspect", "--tools", "read,bash,read"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["model_tools"] == ["read", "bash"]
    assert captured["allow_tools"] == []


# 功能：快捷打印模式将管道正文与任务说明合并后提交给 Agent
# 设计：用非交互 StringIO 模拟 PowerShell 管道，验证 stdin 不会被忽略或拆成第二次运行
def test_print_shorthand_accepts_piped_input(monkeypatch) -> None:
    from io import StringIO

    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(sys, "argv", ["coderook", "--print", "总结以下内容"])
    monkeypatch.setattr(sys, "stdin", StringIO("alpha\nbeta\n"))
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert captured["goal"] == "总结以下内容\n\nInput provided through stdin:\nalpha\nbeta"
    assert captured["display_content"] == "总结以下内容\n\nalpha\nbeta"


# 功能：Windows 管道按实际字节编码读取中文，不把错误解码产生的代理字符送入 JSON
# 设计：分别模拟 PowerShell 7 的 UTF-8 和 cmd 的 GBK 字节流，覆盖两种常见 Windows Shell
@pytest.mark.parametrize("encoding", ["utf-8", "gbk"])
def test_read_piped_stdin_decodes_windows_chinese_bytes(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    from io import BytesIO, TextIOWrapper

    stream = TextIOWrapper(
        BytesIO("中文 PIPE_OK\n".encode(encoding)),
        encoding="gbk",
        errors="surrogateescape",
    )
    monkeypatch.setattr(sys, "stdin", stream)

    assert cli_main._read_piped_stdin() == "中文 PIPE_OK"


# 功能：快捷打印模式把带空格的 @文件 参数转换为有界工作区文件内容
# 设计：保留 argv 的参数边界并分别检查模型与展示输入，避免含空格路径被错误拆分或泄露增强文本到界面
def test_print_shorthand_resolves_explicit_file_argument(
    monkeypatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    target = tmp_path / "design notes.md"
    target.write_text("do not inline", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["coderook", "--print", "@design notes.md", "总结"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda goal, _config, **kwargs: captured.update({"goal": goal, **kwargs}),
    )

    assert cli_main.main() == 0

    assert '\"design notes.md\"' in str(captured["goal"])
    assert "do not inline" in str(captured["goal"])
    assert "truncated=\"false\"" in str(captured["goal"])
    assert captured["display_content"] == "@design notes.md 总结"


# 功能：验证常规 run 命令会先把 Core 绑定到当前工作区再提交任务
# 设计：替换启动器与执行边界并记录顺序，防止脚本任务误发给其他项目的存量 Core
def test_run_command_binds_core_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CodeRookConfig()
    calls: list[str] = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coderook",
            "run",
            "--goal",
            "查看当前目录",
            "--permission-mode",
            "allow-list",
            "--allow-tool",
            "bash",
        ],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "ensure_core_running",
        lambda passed, *, env_file: calls.append(f"core:{passed is config}:{env_file}"),
    )
    monkeypatch.setattr(
        cli_main,
        "cmd_run",
        lambda *_args, **_kwargs: calls.append("run"),
    )

    result = cli_main.main()

    assert result == 0
    assert calls == ["core:True:None", "run"]


# 功能：验证单会话管理命令也会先启动并绑定当前工作区 Core
# 设计：用 rename 覆盖全部 session 子命令共享的前置分发路径，断言启动发生在业务调用之前
def test_session_command_binds_core_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CodeRookConfig()
    calls: list[str] = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "session", "rename", "sess-example", "New title"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "ensure_core_running",
        lambda passed, *, env_file: calls.append(f"core:{passed is config}:{env_file}"),
    )
    monkeypatch.setattr(
        cli_main,
        "cmd_session_rename",
        lambda *_args, **_kwargs: calls.append("rename"),
    )

    result = cli_main.main()

    assert result == 0
    assert calls == ["core:True:None", "rename"]


# 功能：验证 session compact 解析会话与保留重点后调用统一 Core 命令
# 设计：替换压缩入口记录实参，覆盖新增 CLI 表面且不产生真实模型压缩费用
def test_session_compact_dispatches_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CodeRookConfig()
    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coderook",
            "session",
            "compact",
            "sess-example",
            "--focus",
            "preserve failed tests",
        ],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_session_compact",
        lambda session_id, focus, _config: captured.append((session_id, focus)),
    )

    result = cli_main.main()

    assert result == 0
    assert captured == [("sess-example", "preserve failed tests")]


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
        "explicit_workspace": True,
    }


# 功能：验证从 Agent 源码无参数启动 Web 时先进入隔离欢迎工作区再加载配置
# 设计：替换项目注册表和启动器并捕获配置加载时 cwd，证明源码配置和文件不会成为默认项目
def test_web_command_uses_welcome_workspace_for_protected_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    original = Path.cwd()
    registry = SimpleNamespace(
        is_protected_workspace=lambda path: Path(path) == source,
        prepare_welcome_workspace=lambda: welcome,
    )
    monkeypatch.setattr(sys, "argv", ["coderook", "web", "--no-open"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "ProjectRegistry", lambda: registry)
    monkeypatch.setattr(
        cli_main,
        "get_config",
        lambda: captured.update({"config_cwd": Path.cwd()}) or config,
    )
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_web",
        lambda passed, **kwargs: captured.update(
            {"config": passed, "web_cwd": Path.cwd(), **kwargs}
        )
        or 0,
    )
    try:
        os.chdir(source)
        result = cli_main.main()
    finally:
        os.chdir(original)

    assert result == 0
    assert captured == {
        "config_cwd": welcome,
        "config": config,
        "web_cwd": welcome,
        "no_open": True,
        "env_file": None,
        "explicit_workspace": False,
    }


# 功能：验证从源码目录执行 core start 时在加载配置前进入隔离欢迎目录
# 设计：捕获 get_config 与启动命令的 cwd，防止手工启动 daemon 再由 Web 复用时暴露内部源码
def test_core_start_uses_welcome_workspace_for_protected_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    original = Path.cwd()
    monkeypatch.setattr(sys, "argv", ["coderook", "core", "start"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(
        cli_main.ProjectRegistry,
        "enter_welcome_workspace_if_protected",
        lambda _self: os.chdir(welcome) or welcome,
    )
    monkeypatch.setattr(
        cli_main,
        "get_config",
        lambda: captured.update({"config_cwd": Path.cwd()}) or config,
    )
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_core_start",
        lambda passed: captured.update({"config": passed, "start_cwd": Path.cwd()}),
    )
    try:
        os.chdir(source)
        result = cli_main.main()
    finally:
        os.chdir(original)

    assert result == 0
    assert captured == {
        "config_cwd": welcome,
        "config": config,
        "start_cwd": welcome,
    }


# 功能：验证顶层 status 快捷命令查看 Core 状态而不会作为提示词调用模型
# 设计：替换状态处理器并执行完整 CLI 分发，锁定曾真实产生无意义模型会话的歧义入口
def test_status_shortcut_dispatches_to_core_status(monkeypatch: pytest.MonkeyPatch) -> None:
    config = CodeRookConfig()
    captured: list[CodeRookConfig] = []
    monkeypatch.setattr(sys, "argv", ["coderook", "status"])
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(cli_main, "cmd_core_status", captured.append)

    assert cli_main.main() == 0
    assert captured == [config]


# 功能：验证显式打印模式中的单词 status 仍可作为普通用户提示词
# 设计：直接检查参数预处理边界，防止快捷命令破坏 `coderook -p status` 的既有一次性任务语义
def test_core_shortcut_does_not_capture_explicit_print_prompt() -> None:
    assert cli_main._expand_core_shortcut(["-p", "status"]) == ["-p", "status"]


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
    ensure = MagicMock()
    monkeypatch.setattr(cli_main, "ensure_core_running", ensure)
    monkeypatch.setattr(
        cli_main,
        "cmd_review",
        lambda goal, passed_config, **kwargs: captured.update(
            {"goal": goal, "config": passed_config, **kwargs}
        ),
    )

    cli_main.main()

    ensure.assert_called_once_with(config, env_file=None)
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
    ensure = MagicMock()
    monkeypatch.setattr(cli_main, "ensure_core_running", ensure)
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
    ensure.assert_called_once_with(config, env_file=None)
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


# 功能：验证 Doctor 支持与其他模型命令一致的 --route 参数形式
# 设计：替换真实网络探针并经完整 argparse 分发，固定选项别名传入同一个诊断入口
def test_provider_doctor_accepts_route_option(monkeypatch) -> None:
    config = CodeRookConfig()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook", "doctor", "--route", "aliyun", "--json"],
    )
    monkeypatch.setattr(cli_main, "migrate_legacy_state", lambda: None)
    monkeypatch.setattr(cli_main, "get_config", lambda: config)
    monkeypatch.setattr(cli_main, "setup_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_main,
        "cmd_doctor",
        lambda passed, route_id, *, as_json: captured.update({
            "config": passed,
            "route_id": route_id,
            "as_json": as_json,
        }) or 0,
    )

    result = cli_main.main()

    assert result == 0
    assert captured == {"config": config, "route_id": "aliyun", "as_json": True}


# 功能：验证 CLI 边界把损坏凭据文档转成脱敏非零结果而不泄露原始正文
# 设计：让真实 CredentialStore 在 provider list 分支读取坏文件，断言返回码与 stderr 均稳定
def test_cli_credential_store_error_is_safe_and_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    credential_path = tmp_path / "credentials.json"
    credential_path.write_text('{"api_keys":{"secret":"do-not-print"}', encoding="utf-8")
    os.chmod(credential_path, 0o600)
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

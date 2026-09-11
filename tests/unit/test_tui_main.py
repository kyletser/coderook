from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_rook.core.config import CodeRookConfig, LlmConfig
from code_rook.tui import __main__ as tui_main
from code_rook.tui.app import ConfigSwitch, ModelSwitch


# 保持 TUI 单元测试所在目录不被产品入口切换，项目重定向行为由专项测试覆盖
@pytest.fixture(autouse=True)
def _keep_tui_test_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tui_main.ProjectRegistry,
        "enter_welcome_workspace_if_protected",
        lambda _self: Path.cwd(),
    )


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
    assert tui_main.CodeRookTuiApp.call_args.kwargs["open_session_picker"] is False
    app.run.assert_called_once_with()


# 功能：验证不带 ID 的 -r 请求启动会话选择器，带 ID 时仍精确恢复。
# 设计：分别调用 TUI 装配入口并捕获构造参数，避免启动真实 Textual 终端。
@pytest.mark.parametrize(
    ("resume", "expected_session", "expected_picker", "expected_continue"),
    [("", None, True, False), ("sess-known", "sess-known", False, True)],
)
def test_run_tui_supports_resume_picker_without_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    resume: str,
    expected_session: str | None,
    expected_picker: bool,
    expected_continue: bool,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    args = SimpleNamespace(
        env_file=None,
        no_auto_core=False,
        reuse_existing=False,
        replay=None,
        resume=resume,
        fork=None,
        continue_recent=True,
        continue_explicit=False,
        message=[],
        name=None,
        route=None,
        model=None,
        thinking=None,
        tools=None,
    )
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: SimpleNamespace(active=lambda: None))
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", MagicMock(return_value=app))

    tui_main._run_tui(args)

    kwargs = tui_main.CodeRookTuiApp.call_args.kwargs
    assert kwargs["resume_session_id"] == expected_session
    assert kwargs["open_session_picker"] is expected_picker
    assert kwargs["continue_recent"] is expected_continue


# 功能：恢复快捷键不吞掉后续任务文本，精确恢复改由独立 Session 参数表达。
# 设计：通过真实 argparse 入口覆盖 `-r 任务` 与 `--session ID`，仅替换 Core 和界面边界。
@pytest.mark.parametrize(
    (
        "argv",
        "expected_session",
        "expected_picker",
        "expected_prompt",
        "expected_continue",
    ),
    [
        (["coderook-tui", "-r", "继续修复登录"], None, True, "继续修复登录", False),
        (
            ["coderook-tui", "--session", "sess-known"],
            "sess-known",
            False,
            "",
            True,
        ),
    ],
)
def test_tui_resume_arguments_are_unambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    expected_session: str | None,
    expected_picker: bool,
    expected_prompt: str,
    expected_continue: bool,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: SimpleNamespace(active=lambda: None))
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    kwargs = factory.call_args.kwargs
    assert kwargs["resume_session_id"] == expected_session
    assert kwargs["open_session_picker"] is expected_picker
    assert kwargs["initial_prompt"] == expected_prompt
    assert kwargs["continue_recent"] is expected_continue


# 功能：验证从受保护目录启动 TUI 时复用已运行 Core 的活动项目
# 设计：直接向 TUI 装配入口传入 reuse_existing 标志，断言启动器收到该标志且界面仍正常构造
def test_run_tui_reuses_active_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    args = SimpleNamespace(
        env_file=None,
        no_auto_core=False,
        reuse_existing=True,
        replay=None,
        resume=None,
        continue_recent=True,
        message=[],
        route=None,
        model=None,
        thinking=None,
    )
    ensure = MagicMock()
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: SimpleNamespace(active=lambda: None))
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", ensure)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", MagicMock(return_value=app))

    tui_main._run_tui(args)

    ensure.assert_called_once_with(config, env_file=None, reuse_existing=True)
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


# 功能：验证欢迎工作区裸启动不会自动恢复以前的占位会话
# 设计：让项目注册表仅把当前目录识别为欢迎区，断言默认续接关闭且显式 --continue 语义仍可区分
def test_run_tui_starts_clean_in_welcome_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _WelcomeRegistry:
        # 将本测试当前目录固定识别为欢迎工作区
        def is_welcome_workspace(self, _root: Path) -> bool:
            return True

    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    args = SimpleNamespace(
        env_file=None,
        no_auto_core=False,
        reuse_existing=True,
        replay=None,
        resume=None,
        continue_recent=True,
        continue_explicit=False,
        message=[],
        route=None,
        model=None,
        thinking=None,
    )
    monkeypatch.setattr(tui_main, "ProjectRegistry", _WelcomeRegistry)
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: SimpleNamespace(active=lambda: None))
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main._run_tui(args)

    assert factory.call_args.kwargs["continue_recent"] is False
    app.run.assert_called_once_with()


# 功能：TUI 启动参数把显式 route 与模型作为当前会话覆盖交给应用
# 设计：替换 route store 和 Textual 边界，验证启动选择不修改全局活动 Provider
def test_tui_main_passes_initial_model_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    route = SimpleNamespace(id="route-a", model="default-a", catalog_id="catalog-a")
    route_store = MagicMock()
    route_store.active.return_value = route
    route_store.get.return_value = route
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook-tui", "--provider", "route-a", "--model", "model-b"],
    )
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: route_store)
    monkeypatch.setattr(tui_main, "list_models", lambda *_args: ["default-a", "model-b"])
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    assert factory.call_args.kwargs["initial_route_id"] == "route-a"
    assert factory.call_args.kwargs["initial_model"] == "model-b"
    app.run.assert_called_once_with()


# 功能：TUI 支持通过 route/model 单参数选择已配置路由及模型
# 设计：复用启动入口并断言应用收到拆分后的值，覆盖交互模式与打印模式的一致性
def test_tui_main_accepts_provider_prefixed_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    route = SimpleNamespace(id="aliyun", model="qwen-default", catalog_id="aliyun")
    route_store = MagicMock()
    route_store.active.return_value = route
    route_store.get.return_value = route
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook-tui", "--model", "aliyun/qwen3.8-flash"],
    )
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: route_store)
    monkeypatch.setattr(tui_main, "list_models", lambda *_args: ["qwen-default"])
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    assert factory.call_args.kwargs["initial_route_id"] == "aliyun"
    assert factory.call_args.kwargs["initial_model"] == "qwen3.8-flash"
    app.run.assert_called_once_with()


# 功能：TUI 入口把位置参数合并为连接成功后自动提交的一条初始任务。
# 设计：隔离 Core 与 Textual，仅检查构造参数，避免测试中发起真实模型请求。
def test_tui_main_passes_initial_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "coderook-tui", "--fork", "sess-source", "--thinking", "medium",
            "--name", "登录修复", "--tools", "read,bash,read",
            "修复登录", "并运行测试",
        ],
    )
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    assert factory.call_args.kwargs["initial_prompt"] == "修复登录 并运行测试"
    assert factory.call_args.kwargs["initial_session_name"] == "登录修复"
    assert factory.call_args.kwargs["fork_session_id"] == "sess-source"
    assert factory.call_args.kwargs["continue_recent"] is False
    assert factory.call_args.kwargs["initial_thinking_level"] == "medium"
    assert factory.call_args.kwargs["initial_model_tools"] == ["read", "bash"]
    app.run.assert_called_once_with()


# 功能：TUI 启动参数中的显式文件和图片引用会分别进入有界模型上下文与图片附件
# 设计：用临时文本和图片文件隔离真实 Core，只核对入口传给界面的原始展示文本、模型文本和附件路径
def test_tui_main_prepares_initial_file_and_image_references(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "notes.txt"
    image_path = tmp_path / "screen.png"
    text_path.write_text("external context marker", encoding="utf-8")
    image_path.write_bytes(b"image-placeholder")
    config = CodeRookConfig(ipc_token_file=str(tmp_path / "ipc-token"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["coderook-tui", "@notes.txt", "@screen.png", "比较两个附件"],
    )
    monkeypatch.setattr(tui_main, "get_config", lambda: config)
    monkeypatch.setattr(tui_main, "RouteStore", lambda: SimpleNamespace(active=lambda: None))
    monkeypatch.setattr(tui_main, "_setup_logging", lambda _level: None)
    monkeypatch.setattr(tui_main, "ensure_core_running", lambda _config: False)
    monkeypatch.setattr(tui_main, "read_ipc_token", lambda _path: "x" * 32)
    app = MagicMock()
    factory = MagicMock(return_value=app)
    monkeypatch.setattr(tui_main, "CodeRookTuiApp", factory)

    tui_main.main()

    kwargs = factory.call_args.kwargs
    assert kwargs["initial_prompt"] == "@notes.txt @screen.png 比较两个附件"
    assert "external context marker" in kwargs["initial_model_content"]
    assert kwargs["initial_image_paths"] == [image_path]
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

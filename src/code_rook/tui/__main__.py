from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
from pathlib import Path

from code_rook.cli.commands.configure import (
    save_provider_config,
    switch_llm_model,
)
from code_rook.cli.commands.core import (
    CoreLaunchError,
    ensure_core_running,
    validate_core_workspace,
)
from code_rook.core.config import CodeRookConfig, get_config
from code_rook.core.llm.model_catalog import add_model, add_models, list_models
from code_rook.core.llm.route_registry import legacy_config_route
from code_rook.core.llm.route_store import RouteStore
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.transport.auth import IpcTokenError, read_ipc_token
from code_rook.tui.app import CodeRookTuiApp, ConfigSwitch, ModelSwitch

_DEFAULT_TUI_LOG = "~/.coderook/logs/tui.log"


# TUI 文件日志初始化：不写 stderr（避免干扰 Textual 渲染），只写滚动文件
def _setup_logging(level: str) -> None:
    log_path = Path(os.environ.get("CODEROOK_TUI_LOG_FILE", _DEFAULT_TUI_LOG)).expanduser()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter(
            'level=%(levelname)s ts=%(asctime)s source=%(name)s msg="%(message)s"',
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))
    root.handlers.clear()
    root.addHandler(handler)


# 将旧 LLM 配置一次性迁移为显式活动 route，保留原凭据引用而不复制密钥正文
def _ensure_route_configured(
    config: CodeRookConfig,
    route_store: RouteStore | None = None,
) -> None:
    store = route_store or RouteStore()
    if store.list():
        return
    store.add(legacy_config_route(config.llm), activate=True)


# 创建并运行 TUI；配置或模型切换动作返回入口处理
def _run_tui(args: argparse.Namespace) -> ModelSwitch | ConfigSwitch | None:
    config = get_config()
    routes = RouteStore()
    active_route = routes.active()
    _setup_logging(config.logging.level)
    try:
        if args.no_auto_core:
            validate_core_workspace(config)
        else:
            ensure_core_running(config)
    except CoreLaunchError as exc:
        raise SystemExit(f"Core startup error: {exc}") from exc
    try:
        auth_token = read_ipc_token(Path(config.ipc_token_file))
    except IpcTokenError as exc:
        raise SystemExit(f"IPC authentication error: {exc}") from exc
    app = CodeRookTuiApp(
        config.host,
        config.port,
        replay_run_id=args.replay,
        resume_session_id=args.resume,
        continue_recent=args.continue_recent,
        auth_token=auth_token,
        provider=active_route.id if active_route is not None else config.llm.provider,
        model=active_route.model if active_route is not None else config.llm.default_model,
        models=list_models(
            active_route.id if active_route is not None else config.llm.provider,
            active_route.model if active_route is not None else config.llm.default_model,
        ),
        route=active_route.id if active_route is not None else "",
        route_store=routes,
    )
    return app.run()


# coderook-tui 入口：未配置模型也可进入，并支持从 /config 返回后重新加载
def main() -> None:
    migrate_legacy_state()
    parser = argparse.ArgumentParser(prog="coderook-tui", description="CodeRook TUI")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    source.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Resume a saved chat session",
    )
    source.add_argument(
        "--continue",
        dest="continue_recent",
        action="store_true",
        help="Resume the most recently used session in this workspace",
    )
    parser.add_argument(
        "--no-auto-core",
        action="store_true",
        help="Do not automatically start the local Core daemon",
    )
    args = parser.parse_args()

    _ensure_route_configured(get_config())
    while True:
        action = _run_tui(args)
        if isinstance(action, ModelSwitch):
            current = get_config()
            add_model(current.llm.provider, action.model)
            switch_llm_model(current, action.model)
            args.resume = action.session_id
            args.continue_recent = False
        elif isinstance(action, ConfigSwitch):
            current = get_config()
            add_models(action.provider, action.models)
            save_provider_config(
                current,
                action.provider,
                action.api_key,
                action.model,
            )
            args.resume = action.session_id
            args.continue_recent = False
        else:
            break
        from code_rook.cli.commands.core import stop_core

        stop_core(current)
        if args.no_auto_core:
            raise SystemExit(
                "配置已保存；请手动重启 Core 后再次运行 coderook-tui --no-auto-core。"
            )


if __name__ == "__main__":
    main()

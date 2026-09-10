from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
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
from code_rook.core.config import get_config
from code_rook.core.llm.credentials import CredentialStore
from code_rook.core.llm.model_catalog import add_model, add_models, list_models
from code_rook.core.llm.route_store import RouteStore, RouteStoreError
from code_rook.core.processes import mark_agent_process_environment
from code_rook.core.projects import ProjectRegistry
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.transport.auth import IpcTokenError, read_ipc_token
from code_rook.tui.app import CodeRookTuiApp, ConfigSwitch, ModelSwitch

_DEFAULT_TUI_LOG = "~/.coderook/logs/tui.log"
log = logging.getLogger(__name__)


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


# 规范化 TUI 启动时指定的会话名称并拒绝空标题
def _session_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise argparse.ArgumentTypeError("session name must not be empty")
    if len(name) > 200:
        raise argparse.ArgumentTypeError("session name must be at most 200 characters")
    return name


# 创建并运行 TUI；配置或模型切换动作返回入口处理
def _run_tui(args: argparse.Namespace) -> ModelSwitch | ConfigSwitch | None:
    env_file = getattr(args, "env_file", None)
    config = get_config() if env_file is None else get_config(env_file=env_file)
    routes = RouteStore()
    active_route = routes.active()
    requested_route_id = str(getattr(args, "route", "") or "")
    requested_model = str(getattr(args, "model", "") or "")
    if requested_route_id:
        try:
            routes.get(requested_route_id)
        except RouteStoreError as exc:
            raise SystemExit(f"Unknown Provider route: {requested_route_id}") from exc
    elif requested_model:
        if active_route is None:
            raise SystemExit("--model requires --route when no active route is configured")
        requested_route_id = active_route.id
    _setup_logging(config.logging.level)
    try:
        if args.no_auto_core:
            validate_core_workspace(config, env_file=env_file)
        else:
            reuse_existing = bool(getattr(args, "reuse_existing", False))
            if reuse_existing:
                ensure_core_running(
                    config,
                    env_file=env_file,
                    reuse_existing=True,
                )
            elif env_file is None:
                ensure_core_running(config)
            else:
                ensure_core_running(config, env_file=env_file)
    except CoreLaunchError as exc:
        log.error("Core startup failed: %s", exc)
        raise SystemExit("Core startup failed; see the TUI log for details.") from exc
    try:
        auth_token = read_ipc_token(Path(config.ipc_token_file))
    except IpcTokenError as exc:
        log.error("IPC authentication failed: %s", exc)
        raise SystemExit("IPC authentication failed; restart Core and retry.") from exc
    app = CodeRookTuiApp(
        config.host,
        config.port,
        replay_run_id=args.replay,
        resume_session_id=args.resume,
        continue_recent=(
            False
            if ProjectRegistry().is_welcome_workspace(Path.cwd())
            and not bool(getattr(args, "continue_explicit", False))
            and args.resume is None
            else args.continue_recent
        ),
        auth_token=auth_token,
        provider=((active_route.catalog_id or active_route.id) if active_route is not None else ""),
        model=active_route.model if active_route is not None else "",
        models=(
            list_models(active_route.id, active_route.model) if active_route is not None else []
        ),
        route=active_route.id if active_route is not None else "",
        route_store=routes,
        credential_store=CredentialStore(
            env_overlay=config.llm.credential_overlay
        ),
        core_recovery=(
            None
            if args.no_auto_core
            else (
                (
                    lambda: ensure_core_running(
                        config,
                        reuse_existing=bool(getattr(args, "reuse_existing", False)),
                    )
                )
                if env_file is None
                else lambda: ensure_core_running(
                    config,
                    env_file=env_file,
                    reuse_existing=bool(getattr(args, "reuse_existing", False)),
                )
            )
        ),
        initial_prompt=" ".join(getattr(args, "message", [])).strip(),
        initial_session_name=str(getattr(args, "name", "") or ""),
        initial_route_id=requested_route_id,
        initial_model=requested_model,
        initial_thinking_level=getattr(args, "thinking", None),
    )
    return app.run()


# coderook-tui 入口：未配置模型也可进入，并支持从 /config 返回后重新加载
def main() -> None:
    mark_agent_process_environment()
    before_redirect = Path.cwd().resolve()
    redirected = ProjectRegistry().enter_welcome_workspace_if_protected()
    reuse_existing = redirected != before_redirect
    migrate_legacy_state()
    parser = argparse.ArgumentParser(prog="coderook-tui", description="CodeRook TUI")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit environment file; repository .env files are never loaded automatically",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--replay",
        metavar="RUN_ID",
        help="Replay events from a past run on connect",
    )
    source.add_argument(
        "-r", "--resume",
        metavar="SESSION_ID",
        help="Resume a saved chat session",
    )
    source.add_argument(
        "-c", "--continue",
        dest="continue_recent",
        action="store_true",
        default=True,
        help="Resume the most recently used session in this workspace",
    )
    source.add_argument(
        "--new",
        dest="continue_recent",
        action="store_false",
        help="Start a new session instead of resuming this workspace",
    )
    parser.add_argument(
        "--no-auto-core",
        action="store_true",
        help="Do not automatically start the local Core daemon",
    )
    parser.add_argument(
        "--thinking",
        choices=("off", "low", "medium", "high"),
        help="Set the thinking level for the opened session",
    )
    parser.add_argument("--route", help="Provider route for the opened session")
    parser.add_argument("--model", help="Model override for the opened session")
    parser.add_argument(
        "-n", "--name", type=_session_name,
        help="Set the opened session name",
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="Optional initial task submitted after the session is ready",
    )
    args = parser.parse_args()
    args.reuse_existing = reuse_existing
    args.continue_explicit = any(flag in sys.argv[1:] for flag in ("-c", "--continue"))

    while True:
        action = _run_tui(args)
        if isinstance(action, ModelSwitch):
            current = (
                get_config()
                if args.env_file is None
                else get_config(env_file=args.env_file)
            )
            add_model(current.llm.provider, action.model)
            switch_llm_model(current, action.model)
            args.resume = action.session_id
            args.continue_recent = False
        elif isinstance(action, ConfigSwitch):
            current = (
                get_config()
                if args.env_file is None
                else get_config(env_file=args.env_file)
            )
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
            raise SystemExit("配置已保存；请手动重启 Core 后再次运行 coderook-tui --no-auto-core。")


if __name__ == "__main__":
    main()

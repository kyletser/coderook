from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from code_rook.cli.commands.configure import configure_llm
from code_rook.cli.commands.core import CoreLaunchError, ensure_core_running
from code_rook.core.config import get_config
from code_rook.core.llm.credentials import llm_is_configured
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.transport.auth import IpcTokenError, read_ipc_token
from code_rook.tui.app import CodeRookTuiApp

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


# 在交互终端中完成首次 LLM 配置，非交互环境给出明确命令提示
def _ensure_llm_configured() -> None:
    config = get_config()
    if llm_is_configured(config.llm):
        return
    if not sys.stdin.isatty():
        raise SystemExit("LLM is not configured; run `uv run coderook configure` first.")
    print("首次启动需要配置 LLM API。密钥将隐藏输入并保存在 ~/.coderook/credentials.json。\n")
    configure_llm(config)


# 创建并运行 TUI；用户输入 /config 时返回该动作供入口重配后重启
def _run_tui(args: argparse.Namespace) -> str | None:
    config = get_config()
    _setup_logging(config.logging.level)
    if not args.no_auto_core:
        try:
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
        auth_token=auth_token,
    )
    return app.run()


# coderook-tui 入口：首次引导配置并支持从 /config 返回后重新加载
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
    parser.add_argument(
        "--no-auto-core",
        action="store_true",
        help="Do not automatically start the local Core daemon",
    )
    args = parser.parse_args()

    _ensure_llm_configured()
    while _run_tui(args) == "configure":
        config = get_config()
        configure_llm(config)
        from code_rook.cli.commands.core import stop_core

        stop_core()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import sys

from kyle_claude.cli.commands.cancel import cmd_cancel
from kyle_claude.cli.commands.chat import cmd_chat
from kyle_claude.cli.commands.configure import cmd_configure, print_llm_status
from kyle_claude.cli.commands.core import (
    cmd_core_restart,
    cmd_core_start,
    cmd_core_status,
    cmd_core_stop,
)
from kyle_claude.cli.commands.ping import cmd_ping
from kyle_claude.cli.commands.run import cmd_run
from kyle_claude.cli.commands.session import (
    cmd_session_delete,
    cmd_session_export,
    cmd_session_fork,
    cmd_session_rename,
)
from kyle_claude.cli.commands.sessions import cmd_sessions
from kyle_claude.cli.commands.trace import cmd_trace
from kyle_claude.cli.commands.version import cmd_version
from kyle_claude.core.config import get_config
from kyle_claude.core.logging_setup import setup_logging


# CLI 主入口：解析命令行参数并分发到对应子命令
def main() -> None:
    parser = argparse.ArgumentParser(prog="kyle", description="KyleClaude CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ping", help="Ping the core daemon")
    subparsers.add_parser("configure", aliases=["config"], help="Configure the LLM connection")
    subparsers.add_parser("config-status", help="Show the active LLM configuration")
    cancel_parser = subparsers.add_parser("cancel", help="Cancel an active agent run")
    cancel_parser.add_argument("run_id", help="Active run ID")
    chat_parser = subparsers.add_parser("chat", help="Start or resume a chat session")
    chat_parser.add_argument("--resume", metavar="SESSION_ID", help="Resume a saved session")

    sessions_parser = subparsers.add_parser("sessions", help="List saved sessions")
    sessions_parser.add_argument(
        "--all", action="store_true", help="Include closed one-shot and chat sessions"
    )
    sessions_parser.add_argument("--limit", type=int, default=50, choices=range(1, 201))

    session_parser = subparsers.add_parser("session", help="Manage a saved session")
    session_sub = session_parser.add_subparsers(dest="session_command")
    rename_parser = session_sub.add_parser("rename", help="Rename a session")
    rename_parser.add_argument("session_id")
    rename_parser.add_argument("title")
    fork_parser = session_sub.add_parser("fork", help="Fork conversation context")
    fork_parser.add_argument("session_id")
    fork_parser.add_argument("--title", default="")
    export_parser = session_sub.add_parser("export", help="Export conversation and notes")
    export_parser.add_argument("session_id")
    export_parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    export_parser.add_argument("--output", "-o")
    export_parser.add_argument("--force", action="store_true")
    delete_parser = session_sub.add_parser("delete", help="Permanently delete a session")
    delete_parser.add_argument("session_id")
    delete_parser.add_argument("--yes", action="store_true", help="Confirm permanent deletion")

    run_parser = subparsers.add_parser("run", help="Run an agent task")
    run_parser.add_argument("--goal", required=True, help="Goal for the agent to accomplish")
    run_parser.add_argument(
        "--permission-mode",
        choices=("fail-fast", "deny", "allow-list"),
        default="fail-fast",
        help="How a headless run handles tools that require approval",
    )
    run_parser.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        metavar="TOOL",
        help="Tool allowed in allow-list mode; repeat for multiple tools",
    )

    core_parser = subparsers.add_parser("core", help="Manage the core daemon")
    core_sub = core_parser.add_subparsers(dest="core_command")
    core_sub.add_parser("start", help="Start the daemon in the background")
    core_sub.add_parser("stop", help="Stop the running daemon")
    core_sub.add_parser("restart", help="Restart the daemon with the latest configuration")
    core_sub.add_parser("status", help="Show daemon status")

    trace_parser = subparsers.add_parser("trace", help="View system trace log")
    trace_parser.add_argument("run_id", nargs="?", default=None, help="Filter by run ID")
    trace_parser.add_argument("--layer", choices=["ipc", "event", "llm"], help="Filter by layer")
    trace_parser.add_argument("--direction", help="Filter by direction (e.g. CORE→LLM)")
    trace_parser.add_argument("--raw", action="store_true", help="Output raw NDJSON")
    trace_parser.add_argument("--follow", "-f", action="store_true", help="Follow new records")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return

    config = get_config()
    setup_logging(config)

    if args.command in {"configure", "config"}:
        cmd_configure(config)
    elif args.command == "config-status":
        print_llm_status(config)
    elif args.command == "ping":
        cmd_ping(config)
    elif args.command == "cancel":
        cmd_cancel(args.run_id, config)
    elif args.command == "chat":
        cmd_chat(config, args.resume)
    elif args.command == "sessions":
        cmd_sessions(config, include_closed=args.all, limit=args.limit)
    elif args.command == "session":
        if args.session_command == "rename":
            cmd_session_rename(args.session_id, args.title, config)
        elif args.session_command == "fork":
            cmd_session_fork(args.session_id, args.title, config)
        elif args.session_command == "export":
            cmd_session_export(
                args.session_id,
                args.format,
                args.output,
                args.force,
                config,
            )
        elif args.session_command == "delete":
            cmd_session_delete(args.session_id, args.yes, config)
        else:
            session_parser.print_help()
            sys.exit(1)
    elif args.command == "run":
        if args.allow_tool and args.permission_mode != "allow-list":
            parser.error("--allow-tool requires --permission-mode allow-list")
        cmd_run(
            args.goal,
            config,
            permission_mode=args.permission_mode.replace("-", "_"),
            allow_tools=args.allow_tool,
        )
    elif args.command == "core":
        if args.core_command == "start":
            cmd_core_start(config)
        elif args.core_command == "stop":
            cmd_core_stop(config)
        elif args.core_command == "restart":
            cmd_core_restart(config)
        elif args.core_command == "status":
            cmd_core_status(config)
        else:
            core_parser.print_help()
            sys.exit(1)
    elif args.command == "trace":
        cmd_trace(
            args.run_id,
            config,
            layer=args.layer,
            direction=args.direction,
            raw=args.raw,
            follow=args.follow,
        )
    else:
        parser.print_help()
        sys.exit(1)

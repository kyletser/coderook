from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from code_rook.cli.commands.artifacts import cmd_artifacts
from code_rook.cli.commands.cancel import cmd_cancel
from code_rook.cli.commands.chat import cmd_chat
from code_rook.cli.commands.configure import cmd_configure, print_llm_status
from code_rook.cli.commands.core import (
    cmd_core_restart,
    cmd_core_start,
    cmd_core_status,
    cmd_core_stop,
)
from code_rook.cli.commands.doctor import (
    cmd_diagnostic_bundle,
    cmd_doctor,
    cmd_runtime_doctor,
    cmd_system_doctor,
)
from code_rook.cli.commands.memory import cmd_memory
from code_rook.cli.commands.ping import cmd_ping
from code_rook.cli.commands.provider import (
    cmd_model_list,
    cmd_provider_add,
    cmd_provider_edit,
    cmd_provider_list,
    cmd_provider_remove,
    cmd_provider_use,
)
from code_rook.cli.commands.review import cmd_review
from code_rook.cli.commands.run import cmd_run
from code_rook.cli.commands.session import (
    cmd_session_delete,
    cmd_session_export,
    cmd_session_fork,
    cmd_session_rename,
)
from code_rook.cli.commands.sessions import cmd_sessions
from code_rook.cli.commands.skills import (
    cmd_skills_audit,
    cmd_skills_install,
    cmd_skills_list,
    cmd_skills_remove,
    cmd_skills_show,
)
from code_rook.cli.commands.trace import cmd_trace
from code_rook.cli.commands.version import cmd_version
from code_rook.cli.commands.web import cmd_web
from code_rook.core.config import get_config
from code_rook.core.llm.credentials import CredentialStoreError
from code_rook.core.llm.routes import list_route_presets
from code_rook.core.logging_setup import setup_logging
from code_rook.core.state_migration import migrate_legacy_state

_PROVIDER_PRESET_CHOICES = tuple(route.id for route in list_route_presets())


# 将 CLI 标准输出统一为 UTF-8，避免 Windows 管道和脚本模式把中文编码为 GBK
def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


# 在 CLI 进程边界把 typed 凭据故障转换为不含密钥正文的稳定非零结果
def main() -> int:
    _configure_utf8_stdio()
    try:
        return _run_cli()
    except CredentialStoreError as exc:
        print(f"credential store error: {exc.safe_message}", file=sys.stderr)
        return 2


# CLI 主分发器：无参数启动 TUI，其余参数分发到现有子命令
def _run_cli() -> int:
    tui_flags = {"--continue", "--new", "--resume", "--replay", "--no-auto-core"}
    tui_probe = list(sys.argv[1:])
    if tui_probe[:1] == ["--env-file"] and len(tui_probe) >= 2:
        tui_probe = tui_probe[2:]
    explicit_tui = bool(tui_probe) and tui_probe[0] == "tui"
    if not tui_probe or tui_probe[0] in tui_flags or explicit_tui:
        if explicit_tui:
            raw_index = 1 if sys.argv[1] == "tui" else 3
            del sys.argv[raw_index]
        from code_rook.tui.__main__ import main as tui_main

        tui_main()
        return 0

    migrate_legacy_state()
    parser = argparse.ArgumentParser(prog="coderook", description="CodeRook CLI")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit environment file; repository .env files are never loaded automatically",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ping", help="Ping the core daemon")
    web_parser = subparsers.add_parser("web", help="Open the local CodeRook Web workspace")
    web_parser.add_argument(
        "workspace",
        nargs="?",
        type=Path,
        help="Workspace to open; defaults to the current directory",
    )
    web_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Print the one-time launch URL instead of opening a browser",
    )
    subparsers.add_parser("configure", aliases=["config"], help="Configure the LLM connection")
    subparsers.add_parser("config-status", help="Show the active LLM configuration")
    migrate_project = subparsers.add_parser(
        "migrate-project-state",
        help="Explicitly migrate legacy project state into .coderook",
    )
    migrate_project.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the workspace-scoped migration",
    )
    doctor_parser = subparsers.add_parser("doctor", help="Diagnose a provider route")
    doctor_parser.add_argument("route_id", nargs="?", help="Configured route ID")
    doctor_parser.add_argument("--json", action="store_true", help="Print JSON result")
    doctor_parser.add_argument(
        "--repair",
        action="store_true",
        help="With 'doctor runtime', repair safe projection/counter drift",
    )
    doctor_parser.add_argument("--output", type=Path, help="Output path for doctor bundle")
    doctor_parser.add_argument("--yes", action="store_true", help="Confirm bundle export")

    provider_parser = subparsers.add_parser("provider", help="Manage provider routes")
    provider_sub = provider_parser.add_subparsers(dest="provider_command")
    provider_sub.add_parser("list", help="List configured provider routes")
    add_provider = provider_sub.add_parser("add", help="Add a provider route")
    add_provider.add_argument("route_id")
    add_provider.add_argument(
        "--preset",
        choices=_PROVIDER_PRESET_CHOICES,
    )
    add_provider.add_argument(
        "--provider-kind",
        choices=(
            "anthropic",
            "openai",
            "openai-compatible",
            "anthropic-compatible",
            "opencode-zen",
        ),
    )
    add_provider.add_argument(
        "--wire-format",
        choices=("openai_chat", "openai_responses", "anthropic_messages"),
    )
    add_provider.add_argument("--base-url")
    add_provider.add_argument("--model")
    add_provider.add_argument("--temperature", type=float)
    add_provider.add_argument("--credential-ref")
    add_provider.add_argument("--set-key", action="store_true")
    add_provider.add_argument("--activate", action="store_true")

    edit_provider = provider_sub.add_parser("edit", help="Edit a provider route")
    edit_provider.add_argument("route_id")
    edit_provider.add_argument(
        "--provider-kind",
        choices=(
            "anthropic",
            "openai",
            "openai-compatible",
            "anthropic-compatible",
            "opencode-zen",
        ),
    )
    edit_provider.add_argument(
        "--wire-format",
        choices=("openai_chat", "openai_responses", "anthropic_messages"),
    )
    edit_provider.add_argument("--base-url")
    edit_provider.add_argument("--model")
    edit_provider.add_argument("--temperature", type=float)
    edit_provider.add_argument("--credential-ref")
    edit_provider.add_argument("--set-key", action="store_true")
    edit_provider.add_argument("--activate", action="store_true")

    remove_provider = provider_sub.add_parser("remove", help="Remove a provider route")
    remove_provider.add_argument("route_id")
    remove_provider.add_argument("--delete-credential", action="store_true")
    use_provider = provider_sub.add_parser("use", help="Select the active provider route")
    use_provider.add_argument("route_id")
    test_provider = provider_sub.add_parser("test", help="Test a provider route")
    test_provider.add_argument("route_id", nargs="?")
    test_provider.add_argument("--json", action="store_true")

    model_parser = subparsers.add_parser("model", help="Inspect route models")
    model_sub = model_parser.add_subparsers(dest="model_command")
    list_models_parser = model_sub.add_parser("list", help="List configured route models")
    list_models_parser.add_argument("--route", dest="route_id")
    skills_parser = subparsers.add_parser("skills", help="Manage reusable skills")
    skills_sub = skills_parser.add_subparsers(dest="skills_command")
    skills_sub.add_parser("list", help="List skills and provenance")
    show_skill = skills_sub.add_parser("show", help="Show a skill manifest")
    show_skill.add_argument("name")
    install_skill = skills_sub.add_parser("install", help="Preview or install a local skill")
    install_skill.add_argument("source")
    install_skill.add_argument("--scope", choices=("project", "user"), default="project")
    install_skill.add_argument("--trust", action="store_true")
    install_skill.add_argument("--yes", action="store_true")
    install_skill.add_argument("--force", action="store_true")
    remove_skill = skills_sub.add_parser("remove", help="Remove a managed skill")
    remove_skill.add_argument("name")
    remove_skill.add_argument("--scope", choices=("project", "user"), default="project")
    remove_skill.add_argument("--yes", action="store_true")
    skills_sub.add_parser("audit", help="Verify skill digests")

    memory_parser = subparsers.add_parser("memory", help="Manage project memory")
    memory_sub = memory_parser.add_subparsers(dest="memory_command")
    memory_list = memory_sub.add_parser("list", help="List project memories")
    memory_list.add_argument("--active-only", action="store_true")
    memory_list.add_argument("--json", action="store_true")
    memory_add = memory_sub.add_parser("add", help="Add a project memory")
    memory_add.add_argument("name")
    memory_add.add_argument("body")
    memory_add.add_argument("--description", default="")
    memory_add.add_argument(
        "--type",
        dest="memory_type",
        choices=("user", "feedback", "project", "reference"),
        default="project",
    )
    memory_edit = memory_sub.add_parser("edit", help="Edit a project memory")
    memory_edit.add_argument("memory_id")
    memory_edit.add_argument("--name")
    memory_edit.add_argument("--description")
    memory_edit.add_argument(
        "--type",
        dest="memory_type",
        choices=("user", "feedback", "project", "reference"),
    )
    memory_edit.add_argument("--body")
    memory_pin = memory_sub.add_parser("pin", help="Pin a project memory")
    memory_pin.add_argument("memory_id")
    memory_unpin = memory_sub.add_parser("unpin", help="Unpin a project memory")
    memory_unpin.add_argument("memory_id")
    memory_expire = memory_sub.add_parser("expire", help="Set or clear expiry")
    memory_expire.add_argument("memory_id")
    memory_expire.add_argument("expires_at", help="ISO 8601 timestamp or 'never'")
    memory_delete = memory_sub.add_parser("delete", help="Delete a project memory")
    memory_delete.add_argument("memory_id")
    memory_delete.add_argument("--yes", action="store_true")
    memory_auto = memory_sub.add_parser("auto", help="Control Agent memory writes")
    memory_auto.add_argument("mode", choices=("prompt", "off"))
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
    run_parser.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        default="text",
        help="Human output, one final JSON result, or versioned NDJSON events",
    )
    run_parser.add_argument(
        "--event-filter",
        action="append",
        default=[],
        metavar="GLOB",
        help="stream-json event type glob; repeat for multiple patterns",
    )
    run_parser.add_argument(
        "--include-partial",
        action="store_true",
        help="Include llm.token and llm.reasoning events in stream-json",
    )
    run_parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="Append this goal to an existing resumable chat session",
    )
    run_parser.add_argument(
        "--question-mode",
        choices=("fail-fast", "timeout", "preset"),
        default="fail-fast",
        help="How headless runs handle ask_user_question",
    )
    run_parser.add_argument(
        "--question-timeout",
        type=float,
        metavar="SECONDS",
        help="Maximum question wait in timeout mode",
    )
    run_parser.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="TEXT",
        help="Ordered preset answer; repeat for multiple questions",
    )
    review_parser = subparsers.add_parser(
        "review",
        help="Run a read-only structured code review",
    )
    review_parser.add_argument(
        "--goal",
        default="Review the current repository changes for actionable defects.",
    )
    review_parser.add_argument(
        "--output-format",
        choices=("text", "json", "stream-json"),
        default="text",
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

    artifacts_parser = subparsers.add_parser(
        "artifacts",
        help="Inspect or garbage collect artifacts",
    )
    artifacts_sub = artifacts_parser.add_subparsers(dest="artifacts_command")
    artifacts_list = artifacts_sub.add_parser("list", help="List artifact inventory")
    artifacts_list.add_argument("--days", type=int, default=30)
    artifacts_list.add_argument("--json", action="store_true")
    artifacts_gc = artifacts_sub.add_parser("gc", help="Preview or execute artifact GC")
    artifacts_gc.add_argument("--days", type=int, default=30)
    artifacts_gc.add_argument("--yes", action="store_true", help="Confirm deletion")
    artifacts_gc.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.version:
        cmd_version()
        return 0
    if args.command == "migrate-project-state":
        if not args.yes:
            parser.error("migrate-project-state requires --yes")
        report = migrate_legacy_state(include_project=True)
        print(
            "legacy project state: "
            f"found={report.legacy_project_state_found} "
            f"copied={report.project_files_copied}"
        )
        return 0

    if args.command == "web" and args.workspace is not None:
        workspace = args.workspace.expanduser().resolve()
        if not workspace.is_dir():
            parser.error(f"web workspace is not a directory: {workspace}")
        os.chdir(workspace)

    config = get_config() if args.env_file is None else get_config(env_file=args.env_file)
    setup_logging(config)

    if args.command == "web":
        return cmd_web(config, no_open=args.no_open, env_file=args.env_file)
    if args.command in {"configure", "config"}:
        cmd_configure(config)
    elif args.command == "config-status":
        print_llm_status(config)
    elif args.command == "doctor":
        if args.route_id == "runtime":
            return cmd_runtime_doctor(repair=args.repair, as_json=args.json)
        elif args.route_id in {None, "all"}:
            if args.repair:
                parser.error("--repair requires 'coderook doctor runtime'")
            cmd_system_doctor(config, as_json=args.json)
        elif args.route_id == "bundle":
            if args.repair or args.json:
                parser.error("doctor bundle does not support --repair or --json")
            cmd_diagnostic_bundle(
                config,
                args.output or Path.cwd() / "coderook-diagnostics.zip",
                confirmed=args.yes,
            )
        else:
            if args.repair:
                parser.error("--repair requires 'coderook doctor runtime'")
            return cmd_doctor(config, args.route_id, as_json=args.json)
    elif args.command == "provider":
        if args.provider_command == "list":
            cmd_provider_list(config)
        elif args.provider_command == "add":
            cmd_provider_add(
                args.route_id,
                preset=args.preset,
                provider=args.provider_kind,
                wire_format=args.wire_format,
                base_url=args.base_url,
                model=args.model,
                temperature=args.temperature,
                credential_ref=args.credential_ref,
                set_key=args.set_key,
                activate=args.activate,
                validate=True,
                config=config,
            )
        elif args.provider_command == "edit":
            cmd_provider_edit(
                args.route_id,
                provider=args.provider_kind,
                wire_format=args.wire_format,
                base_url=args.base_url,
                model=args.model,
                temperature=args.temperature,
                credential_ref=args.credential_ref,
                set_key=args.set_key,
                activate=args.activate,
                validate=True,
                config=config,
            )
        elif args.provider_command == "remove":
            cmd_provider_remove(
                args.route_id,
                delete_credential=args.delete_credential,
            )
        elif args.provider_command == "use":
            cmd_provider_use(args.route_id, config=config)
        elif args.provider_command == "test":
            return cmd_doctor(config, args.route_id, as_json=args.json)
        else:
            provider_parser.print_help()
            sys.exit(1)
    elif args.command == "model":
        if args.model_command == "list":
            cmd_model_list(config, route_id=args.route_id)
        else:
            model_parser.print_help()
            sys.exit(1)
    elif args.command == "skills":
        if args.skills_command == "list":
            cmd_skills_list()
        elif args.skills_command == "show":
            cmd_skills_show(args.name)
        elif args.skills_command == "install":
            cmd_skills_install(
                args.source,
                scope=args.scope,
                trust=args.trust,
                confirmed=args.yes,
                overwrite=args.force,
            )
        elif args.skills_command == "remove":
            cmd_skills_remove(args.name, scope=args.scope, confirmed=args.yes)
        elif args.skills_command == "audit":
            cmd_skills_audit()
        else:
            skills_parser.print_help()
            sys.exit(1)
    elif args.command == "memory":
        if args.memory_command == "list":
            return cmd_memory(
                config,
                "list",
                params={"include_expired": not args.active_only},
                as_json=args.json,
            )
        if args.memory_command == "add":
            return cmd_memory(
                config,
                "add",
                params={
                    "name": args.name,
                    "body": args.body,
                    "description": args.description,
                    "memory_type": args.memory_type,
                },
            )
        if args.memory_command == "edit":
            changes = {
                key: value
                for key, value in {
                    "name": args.name,
                    "description": args.description,
                    "memory_type": args.memory_type,
                    "body": args.body,
                }.items()
                if value is not None
            }
            return cmd_memory(
                config,
                "edit",
                params={"memory_id": args.memory_id, **changes},
            )
        if args.memory_command in {"pin", "unpin"}:
            return cmd_memory(
                config,
                args.memory_command,
                params={"memory_id": args.memory_id},
            )
        if args.memory_command == "expire":
            return cmd_memory(
                config,
                "expire",
                params={
                    "memory_id": args.memory_id,
                    "expires_at": (
                        None if args.expires_at in {"never", "clear"} else args.expires_at
                    ),
                },
            )
        if args.memory_command == "delete":
            return cmd_memory(
                config,
                "delete",
                params={"memory_id": args.memory_id},
                confirmed=args.yes,
            )
        if args.memory_command == "auto":
            return cmd_memory(
                config,
                "auto",
                params={"auto_save": args.mode},
            )
        memory_parser.print_help()
        return 1
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
        if args.question_mode == "timeout" and args.question_timeout is None:
            parser.error("--question-timeout is required in timeout question mode")
        if args.question_mode == "preset" and not args.answer:
            parser.error("--answer is required in preset question mode")
        cmd_run(
            args.goal,
            config,
            permission_mode=args.permission_mode.replace("-", "_"),
            allow_tools=args.allow_tool,
            output_format=args.output_format,
            event_filters=args.event_filter,
            include_partial=args.include_partial,
            resume_session_id=args.resume,
            question_mode=args.question_mode.replace("-", "_"),
            question_timeout_s=args.question_timeout,
            preset_answers=args.answer,
        )
    elif args.command == "review":
        cmd_review(
            args.goal,
            config,
            output_format=args.output_format,
        )
    elif args.command == "artifacts":
        if args.artifacts_command == "list":
            cmd_artifacts(config, "list", days=args.days, as_json=args.json)
        elif args.artifacts_command == "gc":
            cmd_artifacts(
                config,
                "gc",
                days=args.days,
                confirmed=args.yes,
                as_json=args.json,
            )
        else:
            artifacts_parser.print_help()
            sys.exit(1)
    elif args.command == "core":
        if args.core_command == "start":
            if args.env_file is None:
                cmd_core_start(config)
            else:
                cmd_core_start(config, env_file=args.env_file)
        elif args.core_command == "stop":
            cmd_core_stop(config)
        elif args.core_command == "restart":
            if args.env_file is None:
                cmd_core_restart(config)
            else:
                cmd_core_restart(config, env_file=args.env_file)
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
    return 0

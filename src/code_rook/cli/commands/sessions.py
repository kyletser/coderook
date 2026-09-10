from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, tzinfo
from typing import Any

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient


# 默认隐藏从未运行且没有标题的临时会话，--all 仍返回完整记录
def _visible_sessions(
    sessions: list[dict[str, Any]],
    *,
    include_empty: bool,
) -> list[dict[str, Any]]:
    if include_empty:
        return sessions
    return [
        session
        for session in sessions
        if int(session.get("run_count", 0)) > 0 or str(session.get("title", "")).strip()
    ]


# 将多行或超长会话标题压成适合终端表格的一行摘要
def _display_title(value: object, *, limit: int = 72) -> str:
    title = " ".join(str(value or "").split()) or "(untitled)"
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "…"


# 将 Runtime 的 UTC 时间转换为系统本地时间，旧格式无法解析时保留可读文本
def _display_timestamp(value: object, *, local_timezone: tzinfo | None = None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:19].replace("T", " ")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    localized = parsed.astimezone(local_timezone)
    return localized.strftime("%Y-%m-%d %H:%M:%S")


async def _list_sessions(config: CodeRookConfig, *, include_closed: bool, limit: int) -> int:
    try:
        client = SocketClient.from_config(config)
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1
    except (IpcTokenError, IpcError) as exc:
        print(f"error: IPC authentication failed: {exc}", file=sys.stderr)
        return 1

    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        result = await client.send_command(
            "session.list",
            {"include_closed": include_closed, "limit": limit},
        )
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()

    sessions: list[dict[str, Any]] = _visible_sessions(
        result.get("sessions", []),
        include_empty=include_closed,
    )
    if not sessions:
        print("No sessions found.")
        return 0

    print(f"{'SESSION ID':<20} {'STATUS':<18} {'RUNS':>4}  {'UPDATED':<19}  TITLE")
    for session in sessions:
        updated = _display_timestamp(session.get("updated_at", ""))
        title = _display_title(session.get("title", ""))
        print(
            f"{str(session.get('session_id', '')):<20} "
            f"{str(session.get('status', '')):<18} "
            f"{int(session.get('run_count', 0)):>4}  "
            f"{updated:<19}  {title}"
        )
    return 0


def cmd_sessions(config: CodeRookConfig, *, include_closed: bool, limit: int) -> None:
    try:
        exit_code = asyncio.run(
            _list_sessions(config, include_closed=include_closed, limit=limit)
        )
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)

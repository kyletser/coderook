from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient


async def _call(
    config: CodeRookConfig,
    method: str,
    params: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    try:
        client = SocketClient.from_config(config)
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1, None
    except (IpcTokenError, IpcError) as exc:
        print(f"error: IPC authentication failed: {exc}", file=sys.stderr)
        return 1, None

    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        return 0, await client.send_command(method, params)
    except IpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1, None
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        await client.close()


async def _rename(session_id: str, title: str, config: CodeRookConfig) -> int:
    code, result = await _call(
        config,
        "session.rename",
        {"session_id": session_id, "title": title},
    )
    if result is not None:
        session = result["session"]
        print(f"renamed {session['session_id']}  {session['title']}")
    return code


async def _fork(session_id: str, title: str, config: CodeRookConfig) -> int:
    code, result = await _call(
        config,
        "session.fork",
        {"session_id": session_id, "title": title},
    )
    if result is not None:
        session = result["session"]
        print(f"forked {session_id} -> {session['session_id']}  {session['title']}")
    return code


def _write_export(path: Path, content: str, *, force: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        with path.open("x", encoding="utf-8", newline="\n") as file:
            file.write(content)
        return

    descriptor, raw_temp = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


async def _export(
    session_id: str,
    export_format: str,
    output: str | None,
    force: bool,
    config: CodeRookConfig,
) -> int:
    code, result = await _call(
        config,
        "session.export",
        {"session_id": session_id, "format": export_format},
    )
    if result is None:
        return code
    path = Path(output or str(result["filename"])).expanduser()
    try:
        _write_export(path, str(result["content"]), force=force)
    except FileExistsError:
        print(f"error: output already exists: {path} (use --force)", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: could not write export: {exc}", file=sys.stderr)
        return 1
    print(f"exported {session_id} -> {path}  ({result['media_type']})")
    return 0


async def _delete(session_id: str, confirmed: bool, config: CodeRookConfig) -> int:
    if not confirmed:
        print("error: session deletion is permanent; pass --yes to confirm", file=sys.stderr)
        return 2
    code, result = await _call(
        config,
        "session.delete",
        {"session_id": session_id},
    )
    if result is not None:
        print(f"deleted {result['session_id']}")
    return code


def cmd_session_rename(session_id: str, title: str, config: CodeRookConfig) -> None:
    sys.exit(asyncio.run(_rename(session_id, title, config)))


def cmd_session_fork(session_id: str, title: str, config: CodeRookConfig) -> None:
    sys.exit(asyncio.run(_fork(session_id, title, config)))


def cmd_session_export(
    session_id: str,
    export_format: str,
    output: str | None,
    force: bool,
    config: CodeRookConfig,
) -> None:
    sys.exit(asyncio.run(_export(session_id, export_format, output, force, config)))


def cmd_session_delete(session_id: str, confirmed: bool, config: CodeRookConfig) -> None:
    sys.exit(asyncio.run(_delete(session_id, confirmed, config)))

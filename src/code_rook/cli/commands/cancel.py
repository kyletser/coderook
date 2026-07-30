from __future__ import annotations

import asyncio
import sys

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient


async def _cancel_async(run_id: str, config: CodeRookConfig) -> int:
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
        result = await client.send_command("run.cancel", {"run_id": run_id})
        print(f"cancelled {result.get('run_id', run_id)}")
        return 0
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


def cmd_cancel(run_id: str, config: CodeRookConfig) -> None:
    sys.exit(asyncio.run(_cancel_async(run_id, config)))

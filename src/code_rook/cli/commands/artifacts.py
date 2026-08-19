from __future__ import annotations

import asyncio
import json
import sys
from typing import Any, Literal

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient


# 连接 daemon 并执行 artifact 清单或 GC 命令
async def _artifacts_async(
    config: CodeRookConfig,
    action: Literal["list", "gc"],
    *,
    days: int,
    confirmed: bool,
) -> dict[str, Any]:
    client = SocketClient.from_config(config)
    await client.connect()
    loop_task = asyncio.create_task(client.run_event_loop())
    try:
        method = "artifact.list" if action == "list" else "artifact.gc"
        params: dict[str, Any] = {"days": days}
        if action == "gc":
            params["confirmed"] = confirmed
        return await client.send_command(method, params)
    finally:
        loop_task.cancel()
        await client.close()


# 输出 artifact 管理结果；GC 默认只展示候选，显式确认后才删除
def cmd_artifacts(
    config: CodeRookConfig,
    action: Literal["list", "gc"],
    *,
    days: int = 30,
    confirmed: bool = False,
    as_json: bool = False,
) -> None:
    try:
        result = asyncio.run(
            _artifacts_async(
                config,
                action,
                days=days,
                confirmed=confirmed,
            )
        )
    except (ConnectionRefusedError, OSError, IpcTokenError, IpcError) as exc:
        print(f"error: artifact command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if action == "list":
        artifacts = result.get("artifacts", [])
        for raw in artifacts if isinstance(artifacts, list) else []:
            if not isinstance(raw, dict):
                continue
            flags = []
            if raw.get("referenced"):
                flags.append("referenced")
            if raw.get("gc_candidate"):
                flags.append("candidate")
            print(
                f"{raw.get('sha256', '')}  {raw.get('size', 0)} bytes  "
                f"{raw.get('age_days', 0):.1f} days  {','.join(flags) or '-'}"
            )
        print(
            f"total={result.get('total_bytes', 0)} bytes  "
            f"reclaimable={result.get('reclaimable_bytes', 0)} bytes"
        )
        return
    if result.get("dry_run"):
        print(
            f"dry-run: {len(result.get('candidates', []))} candidate(s), "
            f"{result.get('reclaimable_bytes', 0)} bytes; rerun with --yes to delete"
        )
    else:
        print(
            f"removed {len(result.get('removed', []))} artifact(s), "
            f"reclaimed {result.get('reclaimable_bytes', 0)} bytes"
        )
        print(f"receipt: {result.get('receipt_path', '')}")

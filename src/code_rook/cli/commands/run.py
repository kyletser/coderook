from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient

EXIT_RUN_FAILED = 1
EXIT_PERMISSION_REQUIRED = 3


def _run_finished_exit_code(status: str, reason: str | None) -> int:
    if status == "success":
        return 0
    if reason == "permission_required":
        return EXIT_PERMISSION_REQUIRED
    return EXIT_RUN_FAILED


class StdoutPrinter:
    # 接收 dict 格式的事件并将运行进度格式化打印到终端
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0

    # 若当前行有未换行的 token，补一个换行符
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 根据事件 type 字段分发并格式化打印到 stdout/stderr
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get('tool_name', '')} ok  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} failed  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            reason = event.get("reason") or ""
            reason_text = f"  reason={reason}" if reason else ""
            print(
                f"[run] {event.get('status', '')}  {event.get('steps')} steps  "
                f"{elapsed:.1f}s{reason_text}"
            )


# 异步核心：连接 daemon，订阅事件，触发 run，等待 run.finished
async def _run_async(
    goal: str,
    config: CodeRookConfig,
    *,
    permission_mode: str = "fail_fast",
    allow_tools: list[str] | None = None,
) -> int:
    try:
        client = SocketClient.from_config(config)
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1
    except (IpcTokenError, IpcError) as auth_error:
        print(f"error: IPC authentication failed: {auth_error}", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
    finished = asyncio.Event()
    exit_code = 0

    async def on_event(event: dict[str, Any]) -> None:
        nonlocal exit_code
        await printer.handle(event)
        if event.get("type") == "run.finished":
            exit_code = _run_finished_exit_code(
                str(event.get("status", "")),
                str(event["reason"]) if event.get("reason") else None,
            )
            finished.set()

    client.on_event(on_event)
    loop_task = asyncio.create_task(client.run_event_loop())
    run_id: str | None = None

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": [
                    "run.*",
                    "step.*",
                    "tool.*",
                    "permission.*",
                    "llm.token",
                    "llm.usage",
                ],
                "scope": "global",
            },
        )
        started = await client.send_command(
            "agent.run",
            {
                "goal": goal,
                "permission_mode": permission_mode,
                "allow_tools": allow_tools or [],
            },
        )
        run_id = str(started["run_id"])
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        loop_task.cancel()
        await client.close()
        return 1

    wait_task = asyncio.create_task(finished.wait())
    try:
        done, _pending = await asyncio.wait(
            {wait_task, loop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        if run_id is not None:
            try:
                await asyncio.wait_for(
                    client.send_command("run.cancel", {"run_id": run_id}),
                    timeout=5.0,
                )
                print(f"\n[run] cancelled {run_id}", file=sys.stderr)
            except (IpcError, RuntimeError, OSError, TimeoutError):
                print(f"\nwarning: could not confirm cancellation for {run_id}", file=sys.stderr)
        loop_task.cancel()
        wait_task.cancel()
        await client.close()
        return 130
    if loop_task in done and not finished.is_set():
        exc = loop_task.exception()
        if exc is not None:
            print(f"error: event loop failed: {exc}", file=sys.stderr)
        else:
            print("error: connection closed before run finished", file=sys.stderr)
        await client.close()
        return 1

    loop_task.cancel()
    wait_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    await client.close()
    return exit_code


# 执行 coderook run --goal "..." 命令
def cmd_run(
    goal: str,
    config: CodeRookConfig,
    *,
    permission_mode: str = "fail_fast",
    allow_tools: list[str] | None = None,
) -> None:
    try:
        exit_code = asyncio.run(
            _run_async(
                goal,
                config,
                permission_mode=permission_mode,
                allow_tools=allow_tools,
            )
        )
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)

from __future__ import annotations

import asyncio
import fnmatch
import json
import sys
import time
from typing import Any, Literal

from code_rook.core.config import CodeRookConfig
from code_rook.core.headless import HeadlessEnvelope, HeadlessRunResult
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient

EXIT_RUN_FAILED = 1
EXIT_PERMISSION_REQUIRED = 3
OutputFormat = Literal["text", "json", "stream-json"]
_TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted"}
_PARTIAL_EVENT_TYPES = {"llm.token", "llm.reasoning"}


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

        elif t == "agent.decision" and not event.get("has_visible_text"):
            self._ensure_newline()
            print(f"[{event.get('intent', 'execute')}] {event.get('summary', '')}")

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


class StreamJsonPrinter:
    # 保存事件筛选规则和本地单调序号，保证每行都是独立版本化 envelope
    def __init__(
        self,
        *,
        event_filters: list[str] | None = None,
        include_partial: bool = False,
    ) -> None:
        self._filters = event_filters or ["*"]
        self._include_partial = include_partial
        self._sequence = 0

    # 判断事件是否应进入机器流，默认排除 token/reasoning 增量
    def accepts(self, event_type: str) -> bool:
        if event_type in _PARTIAL_EVENT_TYPES and not self._include_partial:
            return False
        return any(fnmatch.fnmatchcase(event_type, pattern) for pattern in self._filters)

    # 将单个领域事件编码为一行 JSON，stdout 不混入其他文字
    async def handle(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if not self.accepts(event_type):
            return
        self._sequence += 1
        envelope = HeadlessEnvelope(
            kind="event",
            sequence=self._sequence,
            run_id=str(event.get("run_id", "")),
            type=event_type,
            payload=dict(event),
        )
        print(envelope.model_dump_json(), flush=True)

    # 在事件流末尾写入带最终正文和 usage 的稳定结果 envelope
    def write_result(self, result: HeadlessRunResult) -> None:
        self._sequence += 1
        envelope = HeadlessEnvelope(
            kind="result",
            sequence=self._sequence,
            run_id=result.run_id,
            type="run.result",
            payload=result.model_dump(mode="json"),
        )
        print(envelope.model_dump_json(), flush=True)


# 等待 runtime 投影进入终态并读取最终 assistant 正文与持久 usage
async def _fetch_final_result(
    client: SocketClient,
    run_id: str,
    finished_event: dict[str, Any],
) -> HeadlessRunResult:
    turn: dict[str, Any] = {}
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        response = await client.send_command("turn.get", {"turn_id": run_id})
        raw_turn = response.get("turn")
        turn = dict(raw_turn) if isinstance(raw_turn, dict) else {}
        if str(turn.get("status", "")) in _TERMINAL_TURN_STATUSES:
            break
        await asyncio.sleep(0.05)

    result_text = ""
    items_response = await client.send_command("turn.items", {"turn_id": run_id})
    raw_items = items_response.get("items")
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if not isinstance(raw_item, dict) or raw_item.get("kind") != "message":
                continue
            payload = raw_item.get("payload")
            if isinstance(payload, dict) and payload.get("role") == "assistant":
                result_text = str(payload.get("content", ""))

    event_status = str(finished_event.get("status", "failed"))
    turn_status = str(turn.get("status", ""))
    status: Literal["success", "failed", "interrupted"]
    if turn_status == "interrupted":
        status = "interrupted"
    elif event_status == "success":
        status = "success"
    else:
        status = "failed"
    reason_raw = finished_event.get("reason")
    reason = str(reason_raw) if reason_raw else None
    return HeadlessRunResult(
        run_id=run_id,
        thread_id=str(turn.get("thread_id", "")),
        status=status,
        reason=reason,
        exit_code=_run_finished_exit_code(event_status, reason),
        result=result_text,
        steps=int(finished_event.get("steps", 0)),
        usage=dict(turn.get("usage", {})) if isinstance(turn.get("usage"), dict) else {},
    )


# 异步核心：连接 daemon，订阅事件，触发 run，等待 run.finished
async def _run_async(
    goal: str,
    config: CodeRookConfig,
    *,
    permission_mode: str = "fail_fast",
    allow_tools: list[str] | None = None,
    output_format: OutputFormat = "text",
    event_filters: list[str] | None = None,
    include_partial: bool = False,
    resume_session_id: str | None = None,
    question_mode: str = "fail_fast",
    question_timeout_s: float | None = None,
    preset_answers: list[str] | None = None,
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

    text_printer = StdoutPrinter() if output_format == "text" else None
    stream_printer = (
        StreamJsonPrinter(
            event_filters=event_filters,
            include_partial=include_partial,
        )
        if output_format == "stream-json"
        else None
    )
    finished = asyncio.Event()
    exit_code = 0
    finished_event: dict[str, Any] = {}
    early_events: list[dict[str, Any]] = []

    # 统一处理已确认归属的事件：分发给打印机并捕获本 run 的 finished 结果
    async def _process_owned_event(event: dict[str, Any]) -> None:
        nonlocal exit_code, finished_event
        if text_printer is not None:
            await text_printer.handle(event)
        if stream_printer is not None:
            await stream_printer.handle(event)
        if event.get("type") == "run.finished" and str(
            event.get("run_id", "")
        ) == (run_id or ""):
            finished_event = dict(event)
            exit_code = _run_finished_exit_code(
                str(event.get("status", "")),
                str(event["reason"]) if event.get("reason") else None,
            )
            finished.set()

    async def on_event(event: dict[str, Any]) -> None:
        event_run_id = str(event.get("run_id", ""))
        if run_id is None:
            # daemon 是共享长寿命进程：global 订阅会收到其他并发 run 的事件；
            # 本 run 的早期事件可能先于 agent.run 响应到达，先缓冲，拿到 run_id 后回放
            if event_run_id:
                early_events.append(event)
            else:
                await _process_owned_event(event)
            return
        # 缺 run_id 的事件无法归属，仅透传；带 run_id 但不匹配的一律丢弃
        if not _event_belongs_to_run(event, run_id):
            return
        await _process_owned_event(event)

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
                    "agent.*",
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
                "resume_session_id": resume_session_id,
                "question_mode": question_mode,
                "question_timeout_s": question_timeout_s,
                "preset_answers": preset_answers or [],
            },
        )
        run_id = str(started["run_id"])
        for buffered in early_events:
            if _event_belongs_to_run(buffered, run_id):
                await _process_owned_event(buffered)
        early_events.clear()
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

    assert run_id is not None
    try:
        final_result = await _fetch_final_result(client, run_id, finished_event)
        exit_code = final_result.exit_code
        if output_format == "json":
            print(final_result.model_dump_json())
        elif stream_printer is not None:
            stream_printer.write_result(final_result)
    except (IpcError, RuntimeError, OSError, TimeoutError, ValueError) as exc:
        print(f"error: could not read final run result: {exc}", file=sys.stderr)

    loop_task.cancel()
    wait_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass

    await client.close()
    return exit_code


# 判断带运行归属的事件是否属于当前 run；无 run_id 的全局状态事件允许透传
def _event_belongs_to_run(event: dict[str, Any], run_id: str) -> bool:
    event_run_id = str(event.get("run_id", ""))
    return not event_run_id or event_run_id == run_id


# 执行 coderook run --goal "..." 命令
def cmd_run(
    goal: str,
    config: CodeRookConfig,
    *,
    permission_mode: str = "fail_fast",
    allow_tools: list[str] | None = None,
    output_format: OutputFormat = "text",
    event_filters: list[str] | None = None,
    include_partial: bool = False,
    resume_session_id: str | None = None,
    question_mode: str = "fail_fast",
    question_timeout_s: float | None = None,
    preset_answers: list[str] | None = None,
) -> None:
    try:
        exit_code = asyncio.run(
            _run_async(
                goal,
                config,
                permission_mode=permission_mode,
                allow_tools=allow_tools,
                output_format=output_format,
                event_filters=event_filters,
                include_partial=include_partial,
                resume_session_id=resume_session_id,
                question_mode=question_mode,
                question_timeout_s=question_timeout_s,
                preset_answers=preset_answers,
            )
        )
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)

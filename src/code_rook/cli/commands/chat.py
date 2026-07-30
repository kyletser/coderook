from __future__ import annotations

import asyncio
import sys
from typing import Any

from code_rook.core.config import CodeRookConfig
from code_rook.core.transport.auth import IpcTokenError
from code_rook.core.transport.socket_client import IpcError, SocketClient

_DECISION_MAP: dict[str, str] = {
    "y": "allow_once",
    "a": "always_allow",
    "n": "deny_once",
    "d": "always_deny",
}


class ChatPrinter:
    # 初始化 chat 模式的流式输出状态和待审批权限请求
    def __init__(self) -> None:
        self._inline = False
        self.pending_permission_id: str | None = None
        self.active_run_id: str | None = None

    # 若当前 LLM token 尚未换行，则补一个换行
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 按事件类型打印 chat 输出、等待提示和权限审批请求
    async def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")
        if t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True
        elif t == "run.started":
            self.active_run_id = str(event.get("run_id", "")) or None
        elif t == "run.finished":
            if event.get("run_id") == self.active_run_id:
                self.active_run_id = None
        elif t == "tool.call_started":
            self._ensure_newline()
            print(f"[tool] {event.get('tool_name', '')}")
        elif t == "permission.requested":
            self._ensure_newline()
            tool_name = str(event.get("tool_name", ""))
            param_preview = str(event.get("param_preview", ""))
            tool_use_id = str(event.get("tool_use_id", ""))
            print(f"[permission] {tool_name}  {param_preview}")
            print("  y=allow once  a=always allow  n=deny once  d=always deny")
            self.pending_permission_id = tool_use_id
        elif t == "session.waiting_for_input":
            self._ensure_newline()
            self.pending_permission_id = None
            self.active_run_id = None
            print("[waiting for input]")
        elif t == "session.interrupted":
            self._ensure_newline()
            self.pending_permission_id = None
            self.active_run_id = None
            print("[run cancelled]")
        elif t == "session.closed":
            self._ensure_newline()
            print("session closed.")


# 在线程池中读取 stdin，避免阻塞 socket event loop
async def _readline(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, input, prompt)


async def _cancel_active_run(client: SocketClient, printer: ChatPrinter) -> bool:
    run_id = printer.active_run_id
    if run_id is None:
        # run.started normally precedes user-visible work, but allow for a very early Ctrl+C.
        for _ in range(25):
            await asyncio.sleep(0.02)
            run_id = printer.active_run_id
            if run_id is not None:
                break
    if run_id is None:
        print("\n[cancel requested before the run ID was available]", file=sys.stderr)
        return False
    try:
        await asyncio.wait_for(
            client.send_command("run.cancel", {"run_id": run_id}),
            timeout=5.0,
        )
    except (IpcError, RuntimeError, OSError, TimeoutError) as exc:
        print(f"\n[cancel error: {exc}]", file=sys.stderr)
        return False
    return True


# 异步核心：创建或恢复 chat session，循环读取用户输入并处理权限审批
async def _chat_async(config: CodeRookConfig, resume_session_id: str | None = None) -> int:
    try:
        client = SocketClient.from_config(config)
        await client.connect()
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1
    except (IpcTokenError, IpcError) as exc:
        print(f"error: IPC authentication failed: {exc}", file=sys.stderr)
        return 1

    printer = ChatPrinter()
    client.on_event(printer.handle)
    loop_task = asyncio.create_task(client.run_event_loop())

    try:
        await client.send_command(
            "event.subscribe",
            {
                "topics": ["session.*", "run.*", "tool.*", "llm.token", "permission.*"],
                "scope": "global",
            },
        )
        if resume_session_id is None:
            created = await client.send_command("session.create", {"mode": "chat"})
            session_id = str(created["session_id"])
            print(f"[session: {session_id}]")
        else:
            resumed = await client.send_command(
                "session.resume", {"session_id": resume_session_id}
            )
            session = resumed["session"]
            session_id = str(session["session_id"])
            print(f"[resumed: {session_id}] {session.get('title', '')}")

        while True:
            try:
                line = await _readline("> ")
            except (EOFError, KeyboardInterrupt):
                break
            content = line.strip()
            if not content:
                continue

            # 有待审批的权限请求时，将用户输入解释为决策而非聊天消息
            if printer.pending_permission_id:
                decision = _DECISION_MAP.get(content.lower())
                if decision is None:
                    print("  enter y (allow once), a (always allow), "
                          "n (deny once), d (always deny)")
                    continue
                tool_use_id = printer.pending_permission_id
                printer.pending_permission_id = None
                await client.send_command(
                    "permission.respond",
                    {"tool_use_id": tool_use_id, "decision": decision},
                )
                continue

            try:
                await client.send_command(
                    "session.send_message",
                    {"session_id": session_id, "content": content},
                )
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                if await _cancel_active_run(client, printer):
                    print("\n[cancelling current run...]")
                continue

        print(f"\n[session saved: resume with `coderook chat --resume {session_id}`]")
    except IpcError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
        await client.close()
    return 0


# 执行 coderook chat 命令
def cmd_chat(config: CodeRookConfig, resume_session_id: str | None = None) -> None:
    try:
        exit_code = asyncio.run(_chat_async(config, resume_session_id))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)

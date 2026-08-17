"""TUI 与 core 守护进程之间 socket 连接层的抽象。

`TuiConnection` 持有 `SocketClient` 及订阅 topics，负责连接、断线重连、
事件订阅与会话（创建/恢复）的生命周期。它通过回调把事件交回 App，而不直接
驱动 Textual 消息泵。连接期间通过 `self._app._client` 暴露当前客户端，供 App
的 IPC 动作（`send_command`）使用。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, cast

from textual.widgets import Label

from code_rook.core.transport.socket_client import IpcError, SocketClient

log = logging.getLogger(__name__)

# 订阅的全部事件 topic，与 core 的事件总线保持一致
_SUBSCRIBE_TOPICS = [
    "session.*",
    "run.*",
    "step.*",
    "agent.*",
    "tool.*",
    "llm.token",
    "llm.usage",
    "log.*",
    "permission.*",
    "context.*",
    "subagent.*",
    "skill.*",
    "plan.*",
    "user_question.*",
    "lsp.*",
]


class TuiConnection:
    """封装 socket 连接循环，事件经回调回交给 App。"""

    def __init__(
        self,
        app: Any,
        on_event: Any,
        *,
        host: str,
        port: int,
        auth_token: str | None = None,
        client_factory: Any = None,
    ) -> None:
        # 初始化连接参数、事件回调和客户端工厂
        self._app = app
        self._on_event = on_event
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._client_factory = client_factory if client_factory is not None else SocketClient

    # 暴露当前活跃客户端，App 通过它发送 IPC 命令
    @property
    def client(self) -> SocketClient | None:
        return cast(SocketClient | None, self._app._client)

    # 事件分发：调用 App 提供的回调（同步或协程均可），使其不直接接触 socket
    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        result = self._on_event(event)
        if inspect.isawaitable(result):
            await result

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连、会话恢复
    async def run(self) -> None:
        header = self._app.query_one("#header", Label)

        while True:
            client = self._client_factory(
                self._host, self._port, auth_token=self._auth_token
            )
            self._app._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                self._app._update_header("disconnected")
                await asyncio.sleep(2)
                continue
            except IpcError as exc:
                log.error("IPC authentication failed: %s", exc)
                header.update(
                    f"[bold]CodeRook[/bold]  [red]authentication failed: {exc}[/red]"
                )
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._app._client = client
            self._app._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            client.on_event(self._dispatch_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                params: dict[str, Any] = {
                    "topics": list(_SUBSCRIBE_TOPICS),
                    "scope": "global",
                }
                if getattr(self._app, "_replay_run_id", None) is not None:
                    params["replay_from_run"] = self._app._replay_run_id
                await client.send_command("event.subscribe", params)
                if self._app._resume_session_id is None:
                    created = await client.send_command("session.create", {"mode": "chat"})
                    self._app._session_id = str(created["session_id"])
                    self._app._resume_session_id = self._app._session_id
                    self._app._history_loaded = True
                    self._app._session_title = ""
                    self._app._titled = False
                    self._app._first_user_text = ""
                    log.info("session created session_id=%s", self._app._session_id)
                else:
                    resumed = await client.send_command(
                        "session.resume",
                        {"session_id": self._app._resume_session_id},
                    )
                    resumed_info = resumed.get("session", {})
                    resumed_title = (
                        str(resumed_info.get("title", ""))
                        if isinstance(resumed_info, dict)
                        else ""
                    )
                    self._app._session_id = str(resumed["session"]["session_id"])
                    self._app._session_title = resumed_title
                    self._app._titled = bool(
                        resumed_title and resumed_title != "Untitled"
                    )
                    log.info("session resumed session_id=%s", self._app._session_id)
                    if not self._app._history_loaded:
                        history = await client.send_command(
                            "session.get_history",
                            {"session_id": self._app._session_id},
                        )
                        self._app._append_history(history.get("messages", []))
                        self._app._history_loaded = True
                await self._app._refresh_authority()
                self._app._mark_connected()
                await loop_task
            except IpcError as e:
                header.update(f"[bold]CodeRook[/bold]  [red]subscribe error: {e}[/red]")
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._app._client = None
                self._app._session_id = None
                self._app._mark_disconnected()
                self._app._break_llm()
                await client.close()

            self._app._update_header("disconnected")
            await asyncio.sleep(2)
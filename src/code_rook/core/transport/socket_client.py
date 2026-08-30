from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from code_rook.core.bus.commands import CoreAuthenticateResult
from code_rook.core.bus.envelope import JsonRpcError, JsonRpcRequest, JsonRpcSuccess
from code_rook.core.transport.auth import read_ipc_token

if TYPE_CHECKING:
    from code_rook.core.config import CodeRookConfig

type EventHandler = Callable[[dict[str, Any]], Awaitable[None]]

_MAX_LINE_BYTES = 64 * 1024 * 1024  # 64 MB per frame，兼容 MCP 大文件工具结果


class IpcError(RuntimeError):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class SocketClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        auth_token: str | None = None,
        auth_timeout_s: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_handlers: list[EventHandler] = []
        self._auth_token = auth_token
        self._auth_timeout_s = auth_timeout_s

    @classmethod
    def from_config(cls, config: CodeRookConfig) -> SocketClient:
        token = read_ipc_token(Path(config.ipc_token_file))
        return cls(config.host, config.port, auth_token=token)

    # 建立到 core 守护进程的 TCP 连接
    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self._host, self._port, limit=_MAX_LINE_BYTES
        )
        if self._auth_token is not None:
            try:
                await self._authenticate()
            except BaseException:
                await self.close()
                raise

    async def _authenticate(self) -> None:
        assert self._reader is not None
        assert self._writer is not None
        request_id = f"auth-{uuid.uuid4()}"
        request = JsonRpcRequest(
            id=request_id,
            method="core.authenticate",
            params={"token": self._auth_token},
        )
        self._writer.write(request.model_dump_json().encode() + b"\n")
        await self._writer.drain()
        try:
            line = await asyncio.wait_for(
                self._reader.readline(),
                timeout=self._auth_timeout_s,
            )
        except TimeoutError as exc:
            raise IpcError(-1, "IPC authentication timed out") from exc
        if not line:
            raise IpcError(-1, "Core closed the connection during authentication")
        try:
            raw: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IpcError(-1, "Invalid IPC authentication response") from exc
        if "error" in raw:
            error = JsonRpcError.model_validate(raw)
            raise IpcError(error.error.code, error.error.message)
        response = JsonRpcSuccess.model_validate(raw)
        if response.id != request_id:
            raise IpcError(-1, "Mismatched IPC authentication response")
        CoreAuthenticateResult.model_validate(response.result)

    # 关闭 TCP 连接并等待底层 socket 释放
    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except TimeoutError:
                pass
        self._reader = None
        self._writer = None

    # 注册服务器推送事件的回调，可多次调用以添加多个 handler
    def on_event(self, handler: EventHandler) -> None:
        self._event_handlers.append(handler)

    # 发送 JSON-RPC 命令并等待响应，成功返回 result dict，失败抛出 IpcError
    async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("not connected — call connect() first")
        req_id = str(uuid.uuid4())
        request = JsonRpcRequest(id=req_id, method=method, params=params)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self._writer.write(request.model_dump_json().encode() + b"\n")
        await self._writer.drain()
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    # 持续读取服务器消息，分发 RPC 响应到 pending future 或事件到 event handler
    async def run_event_loop(self) -> None:
        if self._reader is None:
            raise RuntimeError("not connected — call connect() first")
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except (ConnectionResetError, OSError):
                    break
                except (ValueError, asyncio.LimitOverrunError):
                    # 单行超出 limit；丢弃本行，继续读取后续消息
                    continue
                if not line:
                    break
                await self._dispatch(line)
        finally:
            # 连接死亡时以 IpcError 完成挂起请求而不是 cancel()：裸 CancelledError 会与
            # 调用方自身的取消不可区分，绕过前端的错误处理（busy 卡死、草稿丢失）
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(IpcError(-1, "connection closed"))
            self._pending.clear()

    # 解析单行消息并路由到 pending future（RPC 响应）或 event handler（服务器推送）
    async def _dispatch(self, line: bytes) -> None:
        try:
            msg: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            return

        if "jsonrpc" in msg:
            req_id: str | None = msg.get("id")
            if req_id and req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(
                            IpcError(err.get("code", -1), err.get("message", "unknown"))
                        )
                    else:
                        fut.set_result(msg.get("result") or {})
        elif msg.get("kind") == "event":
            event_data: dict[str, Any] = msg.get("event", {})
            for handler in self._event_handlers:
                await handler(event_data)

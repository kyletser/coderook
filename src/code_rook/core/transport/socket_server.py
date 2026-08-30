from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import partial
from typing import Any

from pydantic import BaseModel, ValidationError

from code_rook.core.bus.commands import CoreAuthenticateCommand, CoreAuthenticateResult
from code_rook.core.bus.envelope import (
    AUTH_FAILED,
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    HandlerError,
    JsonRpcError,
    JsonRpcRequest,
    JsonRpcSuccess,
    make_error,
)
from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.writer import TraceWriter
from code_rook.core.transport.auth import is_loopback_host, require_loopback_host
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster

logger = logging.getLogger(__name__)

type CommandHandler = Callable[[dict[str, Any]], Awaitable[Any]]

# 每个连接处理协程中，当前正在处理的 writer（供 handler 读取连接上下文）
_writer_var: ContextVar[asyncio.StreamWriter] = ContextVar("_writer_var")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 返回当前 handler 调用所属连接的 StreamWriter
def get_connection_writer() -> asyncio.StreamWriter:
    return _writer_var.get()

_MAX_LINE_BYTES = 64 * 1024 * 1024  # 64 MB per frame，兼容 MCP 大文件工具结果


class SocketServer:
    def __init__(
        self,
        host: str,
        port: int,
        broadcaster: IpcEventBroadcaster | None = None,
        trace: TraceWriter | None = None,
        auth_token: str | None = None,
        auth_timeout_s: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._handlers: dict[str, CommandHandler] = {}
        self._server: asyncio.AbstractServer | None = None
        self._broadcaster = broadcaster
        self._trace = trace
        self._auth_token = auth_token
        self._auth_timeout_s = auth_timeout_s
        self._active_writers: set[asyncio.StreamWriter] = set()

    # 注册一个方法名对应的命令处理函数
    def register(self, method: str, handler: CommandHandler) -> None:
        self._handlers[method] = handler

    # 启动 TCP 服务器；若端口已被占用则退出进程
    async def start(self) -> str:
        require_loopback_host(self._host)
        try:
            _r, w = await asyncio.open_connection(self._host, self._port)
            w.close()
            await w.wait_closed()
            raise SystemExit(f"core already running at {self._host}:{self._port}")
        except (ConnectionRefusedError, OSError):
            pass

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._host,
            port=self._port,
            limit=_MAX_LINE_BYTES,
        )
        return f"{self._host}:{self._port}"

    # 关闭服务器：先断开所有活跃连接，再等待服务器完全关闭（最多 2 秒）
    async def stop(self) -> None:
        if self._server is None:
            return
        for writer in list(self._active_writers):
            try:
                writer.close()
            except Exception:
                pass
        self._server.close()
        try:
            await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass

    # 处理单个客户端连接，完成后关闭写流
    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", "<unknown>")
        logger.debug("client connected: %s", peer)
        self._active_writers.add(writer)
        command_tasks: set[asyncio.Task[None]] = set()
        connection_closed = asyncio.Event()
        try:
            if not self._peer_is_loopback(writer):
                logger.warning("rejected non-loopback IPC peer")
                return
            if self._auth_token is not None:
                authenticated = await self._authenticate_connection(reader, writer)
                if not authenticated:
                    return
            await self._read_loop(
                reader,
                writer,
                command_tasks,
                connection_closed,
            )
        finally:
            connection_closed.set()
            self._active_writers.discard(writer)
            if self._broadcaster is not None:
                self._broadcaster.unsubscribe(writer)
            try:
                writer.close()
            except Exception:
                pass
            logger.debug("client disconnected: %s", peer)

    @staticmethod
    def _peer_is_loopback(writer: asyncio.StreamWriter) -> bool:
        peer = writer.get_extra_info("peername")
        if not isinstance(peer, tuple) or not peer:
            return True
        return is_loopback_host(str(peer[0]))

    async def _authenticate_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=self._auth_timeout_s)
        except TimeoutError:
            await self._send(
                writer,
                make_error(None, AUTH_REQUIRED, "Authentication required"),
                trace=False,
            )
            return False
        except (ValueError, asyncio.LimitOverrunError):
            await self._send(
                writer,
                make_error(None, AUTH_FAILED, "Authentication failed"),
                trace=False,
            )
            return False

        if not line:
            return False
        try:
            raw: Any = json.loads(line)
            request = JsonRpcRequest.model_validate(raw)
        except (json.JSONDecodeError, ValidationError):
            await self._send(
                writer,
                make_error(None, AUTH_FAILED, "Authentication failed"),
                trace=False,
            )
            return False
        if request.method != "core.authenticate":
            await self._send(
                writer,
                make_error(request.id, AUTH_REQUIRED, "Authentication required"),
                trace=False,
            )
            return False
        try:
            command = CoreAuthenticateCommand.model_validate(request.params)
        except ValidationError:
            await self._send(
                writer,
                make_error(request.id, AUTH_FAILED, "Authentication failed"),
                trace=False,
            )
            return False
        assert self._auth_token is not None
        if not hmac.compare_digest(command.token, self._auth_token):
            await self._send(
                writer,
                make_error(request.id, AUTH_FAILED, "Authentication failed"),
                trace=False,
            )
            return False

        await self._send(
            writer,
            JsonRpcSuccess(
                id=request.id,
                result=CoreAuthenticateResult().model_dump(),
            ),
            trace=False,
        )
        return True

    # 持续读取换行分隔的 JSON 行并逐行分发处理
    async def _read_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        command_tasks: set[asyncio.Task[None]],
        connection_closed: asyncio.Event,
    ) -> None:
        while True:
            try:
                line = await reader.readline()
            except asyncio.LimitOverrunError:
                await self._send(writer, make_error(None, INVALID_REQUEST, "Request too large"))
                return
            except ValueError:
                # asyncio 在帧超限（含 limit_size 场景）时统一抛 ValueError 而非 LimitOverrunError，
                # 不捕获会让超大帧变成无声断连，64MB 帧防护的预定的错误响应也发不出去
                await self._send(writer, make_error(None, INVALID_REQUEST, "Request too large"))
                return

            if not line:
                return

            # 每条命令独立作为 task 执行，避免长时间运行的 handler（如 session.send_message）
            # 阻塞读循环，使 permission.respond 等并发命令能被及时处理
            task = asyncio.create_task(self._handle_line(line, writer))
            command_tasks.add(task)
            task.add_done_callback(
                partial(
                    self._command_task_done,
                    writer=writer,
                    connection_closed=connection_closed,
                    command_tasks=command_tasks,
                )
            )

    # 回收独立命令任务，并在连接已关闭时再次清理任务结束前新建的订阅
    def _command_task_done(
        self,
        task: asyncio.Task[None],
        *,
        writer: asyncio.StreamWriter,
        connection_closed: asyncio.Event,
        command_tasks: set[asyncio.Task[None]],
    ) -> None:
        command_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("command task failed after connection cleanup", exc_info=True)
        if connection_closed.is_set() and self._broadcaster is not None:
            self._broadcaster.unsubscribe(writer)

    # 解析单行 JSON-RPC 请求并调用对应 handler，将结果或错误写回客户端
    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            raw: Any = json.loads(line)
        except json.JSONDecodeError as e:
            await self._send(writer, make_error(None, PARSE_ERROR, f"Parse error: {e}"))
            return

        try:
            req = JsonRpcRequest.model_validate(raw)
        except ValidationError as e:
            await self._send(writer, make_error(None, INVALID_REQUEST, "Invalid Request", str(e)))
            return

        if self._trace is not None:
            client_id = str(writer.get_extra_info("peername", "<unknown>"))
            self._trace.emit(
                TraceRecord(
                    ts=_now(),
                    direction="CLIENT→CORE",
                    layer="ipc",
                    kind="command",
                    client_id=client_id,
                    data={"method": req.method, "id": req.id, "params": req.params},
                )
            )

        handler = self._handlers.get(req.method)
        if handler is None:
            await self._send(
                writer,
                make_error(req.id, METHOD_NOT_FOUND, f"Method not found: {req.method}"),
            )
            return

        _writer_var.set(writer)
        try:
            result = await handler(req.params)
        except HandlerError as e:
            await self._send(writer, make_error(req.id, e.code, str(e), e.data))
            return
        except ValidationError as e:
            await self._send(
                writer,
                make_error(req.id, INVALID_REQUEST, "Invalid params", str(e)),
            )
            return
        except Exception as e:
            logger.exception("handler %s raised: %s", req.method, e)
            await self._send(writer, make_error(req.id, INTERNAL_ERROR, "Internal error"))
            return

        result_data: Any = result.model_dump() if isinstance(result, BaseModel) else result
        try:
            await self._send(writer, JsonRpcSuccess(id=req.id, result=result_data))
        except (ConnectionResetError, BrokenPipeError, OSError):
            logger.debug("client disconnected before response for %s", req.method)

    # 将 pydantic 消息序列化为 JSON 行并写入流，随后刷新缓冲区
    async def _send(
        self,
        writer: asyncio.StreamWriter,
        msg: BaseModel,
        *,
        trace: bool = True,
    ) -> None:
        writer.write(msg.model_dump_json().encode() + b"\n")
        await writer.drain()
        if trace and self._trace is not None:
            kind = "error" if isinstance(msg, JsonRpcError) else "response"
            client_id = str(writer.get_extra_info("peername", "<unknown>"))
            self._trace.emit(
                TraceRecord(
                    ts=_now(),
                    direction="CORE→CLIENT",
                    layer="ipc",
                    kind=kind,
                    client_id=client_id,
                    data=msg.model_dump(),
                )
            )

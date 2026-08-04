from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, TypeAdapter

from code_rook.core.api.auth import bearer_authorized, validate_api_binding
from code_rook.core.api.service import RuntimeApiService
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.runtime.store import RecordNotFoundError

logger = logging.getLogger(__name__)
_MAX_HEADERS = 64 * 1024
_MAX_BODY = 1024 * 1024
_THREAD_TURNS = re.compile(r"/v1/threads/([^/]+)/turns")
_THREAD_EVENTS = re.compile(r"/v1/threads/([^/]+)/events")
_TURN_ACTION = re.compile(r"/v1/turns/([^/]+)/(interrupt|steer)")
_TURN_READ = re.compile(r"/v1/turns/([^/]+)/(items|receipt)")
_ANY_JSON = TypeAdapter(Any)


@dataclass(frozen=True)
class _HttpRequest:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


# 将 Pydantic 或普通 JSON 对象编码为 UTF-8
def _json_bytes(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        return value.model_dump_json().encode("utf-8")
    return _ANY_JSON.dump_json(value)


# 校验请求 JSON body 为对象
def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


class HttpApiServer:
    # 初始化有界 HTTP/1.1 API server
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        service: RuntimeApiService,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._service = service
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()

    # 校验绑定安全条件并启动监听
    async def start(self) -> tuple[str, int]:
        validate_api_binding(self._host, self._token)
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        socket = self._server.sockets[0]
        address = socket.getsockname()
        return str(address[0]), int(address[1])

    # 停止接受请求并关闭所有普通与 SSE 连接
    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        writers = list(self._clients)
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(
                *(writer.wait_closed() for writer in writers),
                return_exceptions=True,
            )
        self._clients.clear()

    # 读取并路由单个 HTTP 请求，响应后关闭普通连接
    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._clients.add(writer)
        streaming = False
        try:
            request = await self._read_request(reader)
            if not bearer_authorized(request.headers.get("authorization"), self._token):
                await self._send_json(writer, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            path = urlsplit(request.target).path
            if request.method == "GET" and _THREAD_EVENTS.fullmatch(path):
                streaming = True
                await self._stream_events(reader, writer, request)
                return
            status, payload = await self._dispatch(request)
            await self._send_json(writer, status, payload)
        except asyncio.IncompleteReadError:
            return
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError) as exc:
            await self._send_json(writer, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RecordNotFoundError as exc:
            await self._send_json(writer, HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except HandlerError as exc:
            await self._send_json(writer, HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ConnectionError, BrokenPipeError):
            return
        except Exception:
            logger.exception("HTTP API request failed")
            await self._send_json(
                writer,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error"},
            )
        finally:
            self._clients.discard(writer)
            if not streaming or not writer.is_closing():
                writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    # 解析有界请求行、headers 和 Content-Length body
    async def _read_request(self, reader: asyncio.StreamReader) -> _HttpRequest:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as exc:
            raise ValueError("request headers are too large") from exc
        if len(head) > _MAX_HEADERS:
            raise ValueError("request headers are too large")
        lines = head[:-4].decode("iso-8859-1").split("\r\n")
        request_line = lines[0].split()
        if len(request_line) != 3 or request_line[2] != "HTTP/1.1":
            raise ValueError("invalid HTTP/1.1 request line")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                raise ValueError("invalid HTTP header")
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        if "transfer-encoding" in headers:
            raise ValueError("transfer-encoding is not supported")
        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content-length") from exc
        if not (0 <= content_length <= _MAX_BODY):
            raise ValueError("request body is too large")
        body = await reader.readexactly(content_length) if content_length else b""
        return _HttpRequest(
            method=request_line[0],
            target=request_line[1],
            headers=headers,
            body=body,
        )

    # 分发非流式 v1 路由到统一 runtime service
    async def _dispatch(self, request: _HttpRequest) -> tuple[HTTPStatus, Any]:
        path = urlsplit(request.target).path
        if request.method == "GET" and path == "/v1/threads":
            return HTTPStatus.OK, await self._service.list_threads()
        if request.method == "POST" and path == "/v1/threads":
            body = _json_object(request.body)
            mode = str(body.get("mode", "chat"))
            if mode not in {"chat", "one_shot"}:
                raise ValueError("mode must be chat or one_shot")
            thread = await self._service.create_thread(
                str(body.get("title", "")),
                mode,
            )
            return HTTPStatus.CREATED, thread
        match = _THREAD_TURNS.fullmatch(path)
        if request.method == "POST" and match:
            body = _json_object(request.body)
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            mode = RuntimeMode(str(body.get("mode", RuntimeMode.ACT.value)))
            turn = await self._service.create_turn(match.group(1), content, mode)
            return HTTPStatus.ACCEPTED, turn
        match = _TURN_ACTION.fullmatch(path)
        if request.method == "POST" and match:
            if match.group(2) == "interrupt":
                return HTTPStatus.OK, await self._service.interrupt_turn(match.group(1))
            body = _json_object(request.body)
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            return HTTPStatus.OK, await self._service.steer_turn(match.group(1), content)
        match = _TURN_READ.fullmatch(path)
        if request.method == "GET" and match:
            if match.group(2) == "items":
                return HTTPStatus.OK, await self._service.list_items(match.group(1))
            return HTTPStatus.OK, await self._service.get_receipt(match.group(1))
        if request.method == "GET" and path == "/v1/capabilities":
            return HTTPStatus.OK, await self._service.capabilities()
        if request.method == "GET" and path == "/v1/usage":
            return HTTPStatus.OK, await self._service.usage()
        return HTTPStatus.NOT_FOUND, {"error": "route not found"}

    # 发送带明确长度的 JSON 响应
    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: HTTPStatus,
        value: Any,
    ) -> None:
        if writer.is_closing():
            return
        body = _json_bytes(value)
        header = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(header + body)
        await writer.drain()

    # 从 durable seq 游标持续输出 SSE，断线重连不会跳过已提交事件
    async def _stream_events(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _HttpRequest,
    ) -> None:
        match = _THREAD_EVENTS.fullmatch(urlsplit(request.target).path)
        assert match is not None
        query = parse_qs(urlsplit(request.target).query)
        raw_cursor = query.get("after_seq", [request.headers.get("last-event-id", "0")])[0]
        cursor = int(raw_cursor)
        if cursor < 0:
            raise ValueError("after_seq must be non-negative")
        await self._service.ensure_thread(match.group(1))
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )
        await writer.drain()
        last_heartbeat = time.monotonic()
        disconnected = asyncio.create_task(reader.read(1), name="api-sse-disconnect")
        try:
            while not writer.is_closing() and not disconnected.done():
                events = await self._service.list_events(match.group(1), cursor)
                if events:
                    for event in events:
                        data = event.model_dump_json()
                        writer.write(
                            f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n".encode()
                        )
                        cursor = event.seq
                    await writer.drain()
                    last_heartbeat = time.monotonic()
                    continue
                if time.monotonic() - last_heartbeat >= 15.0:
                    writer.write(b": keepalive\n\n")
                    await writer.drain()
                    last_heartbeat = time.monotonic()
                await asyncio.wait({disconnected}, timeout=0.2)
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

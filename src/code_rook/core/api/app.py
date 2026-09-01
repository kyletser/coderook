from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, TypeAdapter

from code_rook.core.api.auth import (
    bearer_authorized,
    is_loopback_host,
    validate_api_binding,
)
from code_rook.core.api.service import RuntimeApiService
from code_rook.core.api.web_auth import WebAuthManager, WebSession
from code_rook.core.artifacts.image import ImageArtifactInput
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.compatibility import HTTP_API_VERSION
from code_rook.core.configuration import ConfigurationValidationError
from code_rook.core.runtime.store import RecordNotFoundError

logger = logging.getLogger(__name__)
_MAX_HEADERS = 64 * 1024
_MAX_BODY = 3 * 1024 * 1024
_REQUEST_READ_TIMEOUT_S = 15.0
_THREAD_TURNS = re.compile(r"/v1/threads/([^/]+)/turns")
_THREAD_READ = re.compile(r"/v1/threads/([^/]+)")
_THREAD_EVENTS = re.compile(r"/v1/threads/([^/]+)/events")
_THREAD_QUEUE = re.compile(r"/v1/threads/([^/]+)/queue")
_THREAD_QUEUE_ITEM = re.compile(r"/v1/threads/([^/]+)/queue/([^/]+)(?:/(retry))?")
_THREAD_ACTION = re.compile(r"/v1/threads/([^/]+)/(fork|export|context)")
_THREAD_PLAN = re.compile(r"/v1/threads/([^/]+)/turns/([^/]+)/plan")
_THREAD_CHECKPOINT = re.compile(
    r"/v1/threads/([^/]+)/checkpoints/([^/]+)/(preview|rewind)"
)
_TURN_ACTION = re.compile(r"/v1/turns/([^/]+)/(interrupt|steer)")
_TURN_READ = re.compile(r"/v1/turns/([^/]+)/(items|receipt)")
_TURN_GET = re.compile(r"/v1/turns/([^/]+)")
_PERMISSION_RESPONSE = re.compile(r"/v1/permissions/([^/]+)")
# Web 端决策词表到 PermissionManager 词表的翻译表；manager 另有 always_allow_pattern 仅 TUI 使用
_HTTP_PERMISSION_DECISIONS = {
    "allow_once": "allow_once",
    "allow_session": "session_allow",
    "allow_always": "always_allow",
    "deny_once": "deny_once",
    "deny_session": "session_deny",
    "deny_always": "always_deny",
}
_QUESTION_RESPONSE = re.compile(r"/v1/questions/([^/]+)")
_PROVIDER_ACTION = re.compile(r"/v1/providers/([^/]+)/(activate)")
_PROVIDER_READ = re.compile(r"/v1/providers/([^/]+)")
_ARTIFACT_READ = re.compile(r"/v1/artifacts/([0-9a-f]{64})")
_ANY_JSON = TypeAdapter(Any)
_STATIC_ROOT = Path(__file__).resolve().parents[2] / "web" / "static"
_WEB_MUTATION_HEADER = "x-coderook-csrf"
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
        "script-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


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
        control_dispatcher: Callable[[str, dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._service = service
        self._control_dispatcher = control_dispatcher
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._web_auth = WebAuthManager()
        self._bound_host = host
        self._bound_port = port

    # 校验绑定安全条件并启动监听
    async def start(self) -> tuple[str, int]:
        validate_api_binding(self._host, self._token)
        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        socket = self._server.sockets[0]
        address = socket.getsockname()
        self._bound_host = str(address[0])
        self._bound_port = int(address[1])
        return self._bound_host, self._bound_port

    # 签发仅本机可用的 Web 启动 URL，票据只进入 fragment 而不进入 HTTP 日志
    def issue_web_launch_url(self) -> tuple[str, int]:
        if not is_loopback_host(self._bound_host):
            raise ValueError("CodeRook Web is available only on a loopback API binding")
        ticket = self._web_auth.issue_launch_ticket()
        host = "127.0.0.1" if self._bound_host in {"0.0.0.0", "::"} else self._bound_host
        rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return (
            f"http://{rendered_host}:{self._bound_port}/#launch={ticket}",
            self._web_auth.launch_ttl_seconds,
        )

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
        failed = False
        headers_sent = [False]
        try:
            request = await self._read_request(reader)
            path = urlsplit(request.target).path
            if request.method in {"GET", "HEAD"} and self._is_static_path(path):
                await self._send_static(writer, request.method, path, request.headers)
                return
            if request.method == "POST" and path == "/v1/web/bootstrap":
                await self._bootstrap_web_session(writer, request)
                return
            web_session = self._web_auth.authenticate(request.headers.get("cookie"))
            bearer = bearer_authorized(request.headers.get("authorization"), self._token)
            if not bearer and web_session is None:
                await self._send_json(writer, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if web_session is not None and not bearer:
                self._validate_web_request(request, web_session)
            if request.method == "GET" and path == "/v1/web/session":
                if web_session is None:
                    await self._send_json(
                        writer,
                        HTTPStatus.UNAUTHORIZED,
                        {"error": "browser session cookie is required"},
                    )
                    return
                await self._send_json(
                    writer,
                    HTTPStatus.OK,
                    {
                        "authenticated": True,
                        "csrf_token": web_session.csrf_token,
                        "workspace": str(self._service.workspace_root),
                    },
                )
                return
            if request.method == "GET" and _THREAD_EVENTS.fullmatch(path):
                streaming = True
                await self._stream_events(reader, writer, request, headers_sent)
                return
            status, payload = await self._dispatch(request)
            await self._send_json(writer, status, payload)
        except asyncio.IncompleteReadError:
            return
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, KeyError) as exc:
            failed = True
            await self._send_json_guarded(
                writer, headers_sent, HTTPStatus.BAD_REQUEST, {"error": str(exc)}
            )
        except RecordNotFoundError as exc:
            failed = True
            await self._send_json_guarded(
                writer, headers_sent, HTTPStatus.NOT_FOUND, {"error": str(exc)}
            )
        except ConfigurationValidationError as exc:
            failed = True
            result = exc.result
            await self._send_json_guarded(
                writer,
                headers_sent,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "error": "provider validation failed",
                    "code": "provider_validation_failed",
                    "category": result.category,
                    "message": result.message,
                    "provider_status": result.http_status,
                },
            )
        except HandlerError as exc:
            failed = True
            await self._send_json_guarded(
                writer, headers_sent, HTTPStatus.CONFLICT, {"error": str(exc)}
            )
        except (ConnectionError, BrokenPipeError):
            return
        except Exception:
            failed = True
            logger.exception("HTTP API request failed")
            await self._send_json_guarded(
                writer,
                headers_sent,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal server error"},
            )
        finally:
            self._clients.discard(writer)
            # SSE 流中途失败时也必须关闭连接：否则异常分支留下的半开连接永不回收
            if failed or not streaming or not writer.is_closing():
                writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

    # 安全发送失败响应，SSE 头已发出时只关闭连接而不写入第二个 HTTP 响应
    async def _send_json_guarded(
        self,
        writer: asyncio.StreamWriter,
        headers_sent: list[bool],
        status: HTTPStatus,
        payload: dict[str, object],
    ) -> None:
        if headers_sent[0]:
            logger.error("HTTP API stream failed mid-response; closing without extra response")
            return
        await self._send_json(writer, status, payload)

    # 判断请求是否属于公开静态 Web 壳资源而不是受保护 API
    def _is_static_path(self, path: str) -> bool:
        return (
            path in {"/", "/index.html", "/manifest.webmanifest", "/favicon.svg"}
            or path.startswith("/assets/")
        )

    # 校验静态 Web 请求的 Host 只能指向当前本机监听端口
    def _validate_web_host(self, headers: dict[str, str]) -> str:
        presented = headers.get("host", "").strip().lower()
        allowed = {
            f"{self._bound_host}:{self._bound_port}".lower(),
            f"127.0.0.1:{self._bound_port}",
            f"localhost:{self._bound_port}",
            f"[::1]:{self._bound_port}",
        }
        if presented not in allowed:
            raise ValueError("invalid Web Host header")
        return presented

    # 校验 Cookie 写请求的同源和 CSRF header，读取请求只要求合法 Host
    def _validate_web_request(self, request: _HttpRequest, session: WebSession) -> None:
        host = self._validate_web_host(request.headers)
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        origin = request.headers.get("origin", "").strip().lower()
        if origin != f"http://{host}":
            raise ValueError("invalid Web Origin header")
        if not self._web_auth.csrf_authorized(
            session,
            request.headers.get(_WEB_MUTATION_HEADER),
        ):
            raise ValueError("invalid Web CSRF token")

    # 消费 fragment 传入的一次性票据并设置 HttpOnly 浏览器会话 Cookie
    async def _bootstrap_web_session(
        self,
        writer: asyncio.StreamWriter,
        request: _HttpRequest,
    ) -> None:
        host = self._validate_web_host(request.headers)
        origin = request.headers.get("origin", "").strip().lower()
        if origin != f"http://{host}":
            raise ValueError("invalid Web bootstrap origin")
        body = _json_object(request.body)
        launch_token = body.get("launch_token")
        if not isinstance(launch_token, str) or not launch_token:
            raise ValueError("launch_token is required")
        session = self._web_auth.exchange(launch_token)
        if session is None:
            await self._send_json(
                writer,
                HTTPStatus.UNAUTHORIZED,
                {"error": "launch token is invalid or expired"},
            )
            return
        await self._send_json(
            writer,
            HTTPStatus.OK,
            {
                "authenticated": True,
                "csrf_token": session.csrf_token,
                "workspace": str(self._service.workspace_root),
            },
            extra_headers={"Set-Cookie": self._web_auth.cookie_header(session)},
        )

    # 从打包目录提供固定静态资源并拒绝路径穿越和未知文件
    async def _send_static(
        self,
        writer: asyncio.StreamWriter,
        method: str,
        path: str,
        headers: dict[str, str],
    ) -> None:
        self._validate_web_host(headers)
        relative = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
        candidate = (_STATIC_ROOT / relative).resolve(strict=False)
        try:
            candidate.relative_to(_STATIC_ROOT.resolve())
        except ValueError as exc:
            raise ValueError("invalid static asset path") from exc
        if not candidate.is_file():
            await self._send_json(writer, HTTPStatus.NOT_FOUND, {"error": "asset not found"})
            return
        body = b"" if method == "HEAD" else candidate.read_bytes()
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache_control = (
            "no-store"
            if candidate.name in {"index.html", "manifest.webmanifest"}
            else "public, max-age=31536000, immutable"
        )
        await self._send_response(
            writer,
            HTTPStatus.OK,
            body,
            content_type=media_type,
            declared_length=candidate.stat().st_size,
            extra_headers={"Cache-Control": cache_control},
        )

    # 解析有界请求行、headers 和 Content-Length body
    async def _read_request(self, reader: asyncio.StreamReader) -> _HttpRequest:
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=_REQUEST_READ_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise ValueError("request headers timed out") from exc
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
        if content_length:
            try:
                body = await asyncio.wait_for(
                    reader.readexactly(content_length),
                    timeout=_REQUEST_READ_TIMEOUT_S,
                )
            except TimeoutError as exc:
                raise ValueError("request body timed out") from exc
        else:
            body = b""
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
        match = _THREAD_ACTION.fullmatch(path)
        if match and match.group(2) == "fork" and request.method == "POST":
            body = _json_object(request.body)
            title = body.get("title", "")
            if not isinstance(title, str):
                raise ValueError("title must be a string")
            return HTTPStatus.CREATED, await self._service.fork_thread(
                match.group(1),
                title=title,
            )
        if match and match.group(2) == "export" and request.method == "GET":
            query = parse_qs(urlsplit(request.target).query)
            export_format = query.get("format", ["markdown"])[0]
            if export_format not in {"markdown", "json"}:
                raise ValueError("format must be markdown or json")
            return HTTPStatus.OK, await self._service.export_thread(
                match.group(1),
                cast(Any, export_format),
            )
        if match and match.group(2) == "context" and request.method == "GET":
            return HTTPStatus.OK, await self._service.thread_context(match.group(1))
        queue_match = _THREAD_QUEUE.fullmatch(path)
        if queue_match and request.method == "GET":
            return HTTPStatus.OK, await self._service.list_queued_messages(
                queue_match.group(1)
            )
        if queue_match and request.method == "POST":
            body = _json_object(request.body)
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            raw_mode = str(body.get("mode", RuntimeMode.ACT.value))
            try:
                mode = RuntimeMode(raw_mode)
            except ValueError as exc:
                raise ValueError("invalid runtime mode") from exc
            raw_attachments = body.get("attachments", [])
            if not isinstance(raw_attachments, list):
                raise ValueError("attachments must be a list")
            attachments = [
                ImageArtifactInput.model_validate(item) for item in raw_attachments
            ]
            display_content = body.get("display_content")
            if display_content is not None and not isinstance(display_content, str):
                raise ValueError("display_content must be a string")
            return HTTPStatus.CREATED, await self._service.queue_message(
                queue_match.group(1),
                content,
                mode,
                attachments,
                display_content=display_content,
            )
        queue_item_match = _THREAD_QUEUE_ITEM.fullmatch(path)
        if queue_item_match and request.method == "DELETE" and not queue_item_match.group(3):
            return HTTPStatus.OK, await self._service.remove_queued_message(
                queue_item_match.group(1),
                queue_item_match.group(2),
            )
        if queue_item_match and request.method == "POST" and queue_item_match.group(3):
            return HTTPStatus.OK, await self._service.retry_queued_message(
                queue_item_match.group(1),
                queue_item_match.group(2),
            )
        checkpoint_match = _THREAD_CHECKPOINT.fullmatch(path)
        if checkpoint_match and checkpoint_match.group(3) == "preview":
            if request.method != "GET":
                return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}
            query = parse_qs(urlsplit(request.target).query)
            return HTTPStatus.OK, await self._service.preview_rewind(
                checkpoint_match.group(1),
                checkpoint_match.group(2),
                run_id=query.get("run_id", [None])[0],
            )
        if checkpoint_match and checkpoint_match.group(3) == "rewind":
            if request.method != "POST":
                return HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}
            body = _json_object(request.body)
            if body.get("confirmed") is not True:
                raise ValueError("rewind requires explicit confirmation")
            return HTTPStatus.OK, await self._service.rewind_thread(
                checkpoint_match.group(1),
                checkpoint_match.group(2),
                run_id=(str(body["run_id"]) if body.get("run_id") else None),
                expected_digest=(
                    str(body["expected_digest"])
                    if body.get("expected_digest")
                    else None
                ),
            )
        plan_match = _THREAD_PLAN.fullmatch(path)
        if request.method == "POST" and plan_match:
            body = _json_object(request.body)
            return HTTPStatus.OK, await self._service.respond_plan(
                plan_match.group(1),
                plan_match.group(2),
                str(body.get("decision", "")),
                str(body.get("revision", "")),
            )
        match = _THREAD_READ.fullmatch(path)
        if request.method == "GET" and match:
            return HTTPStatus.OK, await self._service.get_thread(match.group(1))
        if request.method == "PATCH" and match:
            body = _json_object(request.body)
            title = body.get("title")
            archived = body.get("archived")
            if title is not None and not isinstance(title, str):
                raise ValueError("title must be a string")
            if archived is not None and not isinstance(archived, bool):
                raise ValueError("archived must be a boolean")
            return HTTPStatus.OK, await self._service.update_thread(
                match.group(1),
                title=title,
                archived=archived,
            )
        if request.method == "DELETE" and match:
            body = _json_object(request.body)
            if body.get("confirmed") is not True:
                raise ValueError("thread deletion requires explicit confirmation")
            return HTTPStatus.OK, await self._service.delete_thread(match.group(1))
        match = _THREAD_TURNS.fullmatch(path)
        if request.method == "GET" and match:
            query = parse_qs(urlsplit(request.target).query)
            raw_limit = query.get("limit", [""])[0]
            limit = int(raw_limit) if raw_limit else None
            if limit is not None and not (1 <= limit <= 100):
                raise ValueError("limit must be between 1 and 100")
            before_turn_id = query.get("before", [None])[0]
            return HTTPStatus.OK, await self._service.list_turns(
                match.group(1),
                limit=limit,
                before_turn_id=before_turn_id,
            )
        if request.method == "POST" and match:
            body = _json_object(request.body)
            content = body.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("content must be a non-empty string")
            display_content = body.get("display_content")
            if display_content is not None and (
                not isinstance(display_content, str) or not display_content.strip()
            ):
                raise ValueError("display_content must be a non-empty string")
            mode = RuntimeMode(str(body.get("mode", RuntimeMode.ACT.value)))
            raw_attachments = body.get("attachments", [])
            if not isinstance(raw_attachments, list):
                raise ValueError("attachments must be a list")
            attachments = [
                ImageArtifactInput.model_validate(item) for item in raw_attachments
            ]
            turn = await self._service.create_turn(
                match.group(1),
                content,
                mode,
                attachments,
                display_content=display_content,
            )
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
        match = _TURN_GET.fullmatch(path)
        if request.method == "GET" and match:
            return HTTPStatus.OK, await self._service.get_turn(match.group(1))
        if request.method == "GET" and path == "/v1/capabilities":
            return HTTPStatus.OK, await self._service.capabilities()
        if request.method == "GET" and path == "/v1/usage":
            return HTTPStatus.OK, await self._service.usage()
        if request.method == "POST" and path == "/v1/artifacts/images":
            body = _json_object(request.body)
            encoded = body.get("data_base64")
            if not isinstance(encoded, str):
                raise ValueError("data_base64 must be a string")
            return HTTPStatus.CREATED, await self._service.upload_image(encoded)
        artifact_match = _ARTIFACT_READ.fullmatch(path)
        if request.method == "GET" and artifact_match:
            query = parse_qs(urlsplit(request.target).query)
            return HTTPStatus.OK, await self._service.read_artifact(
                artifact_match.group(1),
                offset=int(query.get("offset", ["0"])[0]),
                limit=int(query.get("limit", ["20000"])[0]),
            )
        if request.method == "GET" and path == "/v1/providers":
            return HTTPStatus.OK, await self._service.provider_catalog()
        if request.method == "POST" and path == "/v1/providers":
            return HTTPStatus.CREATED, await self._service.save_provider(
                _json_object(request.body)
            )
        provider_action = _PROVIDER_ACTION.fullmatch(path)
        if request.method == "POST" and provider_action:
            return HTTPStatus.OK, await self._service.activate_provider(
                provider_action.group(1)
            )
        provider_match = _PROVIDER_READ.fullmatch(path)
        if request.method == "DELETE" and provider_match:
            body = _json_object(request.body)
            if body.get("confirmed") is not True:
                raise ValueError("provider deletion requires explicit confirmation")
            return HTTPStatus.OK, await self._service.delete_provider(
                provider_match.group(1),
                delete_credential=bool(body.get("delete_credential", False)),
            )
        if path == "/v1/goals" and request.method in {"GET", "POST"}:
            payload = (
                {
                    "session_id": parse_qs(urlsplit(request.target).query).get(
                        "thread_id", [""]
                    )[0]
                }
                if request.method == "GET"
                else _json_object(request.body)
            )
            return (
                HTTPStatus.OK if request.method == "GET" else HTTPStatus.CREATED,
                await self._dispatch_control(
                    "goal.list" if request.method == "GET" else "goal.create",
                    payload,
                ),
            )
        goal_action = re.fullmatch(r"/v1/goals/([^/]+)/(pause|resume|clear)", path)
        if request.method == "POST" and goal_action:
            return HTTPStatus.OK, await self._dispatch_control(
                f"goal.{goal_action.group(2)}",
                {"goal_id": goal_action.group(1)},
            )
        if path == "/v1/workers" and request.method == "GET":
            query = parse_qs(urlsplit(request.target).query)
            return HTTPStatus.OK, await self._dispatch_control(
                "worker.list",
                {"session_id": query.get("thread_id", [""])[0]},
            )
        worker_action = re.fullmatch(
            r"/v1/workers/([^/]+)/(followup|review|apply|cancel)",
            path,
        )
        if request.method == "POST" and worker_action:
            payload = _json_object(request.body)
            payload["worker_id"] = worker_action.group(1)
            return HTTPStatus.OK, await self._dispatch_control(
                f"worker.{worker_action.group(2)}",
                payload,
            )
        if path == "/v1/mcp" and request.method == "GET":
            return HTTPStatus.OK, await self._dispatch_control("mcp.list", {})
        if path == "/v1/memories" and request.method in {"GET", "POST"}:
            return (
                HTTPStatus.OK if request.method == "GET" else HTTPStatus.CREATED,
                await self._dispatch_control(
                    "memory.list" if request.method == "GET" else "memory.add",
                    {} if request.method == "GET" else _json_object(request.body),
                ),
            )
        memory_action = re.fullmatch(r"/v1/memories/([^/]+)", path)
        if request.method == "PATCH" and memory_action:
            payload = _json_object(request.body)
            payload["memory_id"] = memory_action.group(1)
            return HTTPStatus.OK, await self._dispatch_control(
                "memory.edit",
                payload,
            )
        if request.method == "DELETE" and memory_action:
            body = _json_object(request.body)
            if body.get("confirmed") is not True:
                raise ValueError("memory deletion requires explicit confirmation")
            return HTTPStatus.OK, await self._dispatch_control(
                "memory.delete",
                {"memory_id": memory_action.group(1)},
            )
        if path == "/v1/memory/settings" and request.method in {"GET", "PATCH"}:
            return HTTPStatus.OK, await self._dispatch_control(
                "memory.settings.get" if request.method == "GET" else "memory.settings.set",
                {} if request.method == "GET" else _json_object(request.body),
            )
        if path == "/v1/skills" and request.method == "GET":
            return HTTPStatus.OK, await self._service.list_skills()
        if path == "/v1/skills/install" and request.method == "POST":
            return HTTPStatus.OK, await self._service.install_skill(
                _json_object(request.body)
            )
        skill_match = re.fullmatch(r"/v1/skills/([^/]+)", path)
        if request.method == "DELETE" and skill_match:
            body = _json_object(request.body)
            return HTTPStatus.OK, await self._service.remove_skill(
                skill_match.group(1),
                scope=str(body.get("scope", "project")),
                confirmed=body.get("confirmed") is True,
            )
        match = _PERMISSION_RESPONSE.fullmatch(path)
        if request.method == "POST" and match:
            body = _json_object(request.body)
            decision = str(body.get("decision", ""))
            if decision not in {
                "allow_once", "allow_session", "allow_always",
                "deny_once", "deny_session", "deny_always",
            }:
                raise ValueError("invalid permission decision")
            # Web 词表与 PermissionManager 词表不同构，必须在此翻译，否则会静默变成拒绝
            decision = _HTTP_PERMISSION_DECISIONS[decision]
            raw_hunks = body.get("selected_hunks")
            if raw_hunks is not None and not (
                isinstance(raw_hunks, list)
                and all(isinstance(item, str) for item in raw_hunks)
            ):
                raise ValueError("selected_hunks must be a string list")
            return HTTPStatus.OK, await self._service.respond_permission(
                match.group(1),
                decision,
                session_id=(
                    str(body["session_id"])
                    if body.get("session_id") is not None
                    else None
                ),
                selected_hunks=raw_hunks,
                patch_plan_id=(
                    str(body["patch_plan_id"])
                    if body.get("patch_plan_id") is not None
                    else None
                ),
            )
        question_match = _QUESTION_RESPONSE.fullmatch(path)
        if request.method == "POST" and question_match:
            body = _json_object(request.body)
            answer = body.get("answer")
            if not isinstance(answer, str):
                raise ValueError("answer must be a string")
            return HTTPStatus.OK, await self._service.answer_question(
                question_match.group(1),
                answer,
            )
        if request.method == "GET" and path == "/v1/workspace/files":
            query = parse_qs(urlsplit(request.target).query)
            raw_limit = query.get("limit", ["300"])[0]
            return HTTPStatus.OK, await self._service.list_workspace_files(
                path=query.get("path", ["."])[0],
                query=query.get("query", [""])[0],
                limit=int(raw_limit),
            )
        if request.method == "GET" and path == "/v1/workspace/file":
            query = parse_qs(urlsplit(request.target).query)
            selected = query.get("path", [""])[0]
            return HTTPStatus.OK, await self._service.read_workspace_file(selected)
        if request.method == "GET" and path == "/v1/workspace/diff":
            query = parse_qs(urlsplit(request.target).query)
            scope = query.get("scope", ["all"])[0]
            if scope not in {"all", "staged", "unstaged"}:
                raise ValueError("invalid diff scope")
            return HTTPStatus.OK, await self._service.workspace_diff(
                scope=scope,
                path=query.get("path", ["."])[0],
            )
        if request.method == "POST" and path == "/v1/workspace/stage":
            body = _json_object(request.body)
            paths = body.get("paths")
            if not isinstance(paths, list) or not all(
                isinstance(item, str) for item in paths
            ):
                raise ValueError("paths must be a string list")
            return HTTPStatus.OK, await self._service.workspace_stage(
                str(body.get("thread_id", "")),
                paths,
                expected_digest=str(body.get("expected_digest", "")),
                confirmed=body.get("confirmed") is True,
            )
        if request.method == "POST" and path == "/v1/workspace/commit":
            body = _json_object(request.body)
            return HTTPStatus.OK, await self._service.workspace_commit(
                str(body.get("thread_id", "")),
                str(body.get("message", "")),
                expected_digest=str(body.get("expected_digest", "")),
                confirmed=body.get("confirmed") is True,
            )
        return HTTPStatus.NOT_FOUND, {"error": "route not found"}

    # 把 Web 高级抽屉请求限制在 Core 明确公开的稳定 typed handler 白名单
    async def _dispatch_control(
        self,
        command: str,
        payload: dict[str, Any],
    ) -> Any:
        if self._control_dispatcher is None:
            raise ValueError("advanced control is unavailable")
        return await self._control_dispatcher(command, payload)

    # 发送带明确长度的 JSON 响应
    async def _send_json(
        self,
        writer: asyncio.StreamWriter,
        status: HTTPStatus,
        value: Any,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = _json_bytes(value)
        await self._send_response(
            writer,
            status,
            body,
            content_type="application/json; charset=utf-8",
            extra_headers=extra_headers,
        )

    # 发送带安全响应头和明确长度的普通 HTTP body
    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str,
        declared_length: int | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if writer.is_closing():
            return
        headers = {
            "Content-Type": content_type,
            "X-CodeRook-API-Version": HTTP_API_VERSION,
            "Content-Length": str(len(body) if declared_length is None else declared_length),
            "Connection": "close",
            **_SECURITY_HEADERS,
            **(extra_headers or {}),
        }
        rendered = "".join(f"{name}: {value}\r\n" for name, value in headers.items())
        head = f"HTTP/1.1 {status.value} {status.phrase}\r\n{rendered}\r\n".encode("ascii")
        writer.write(head + body)
        await writer.drain()

    # 从 durable seq 游标持续输出 SSE，断线重连不会跳过已提交事件
    async def _stream_events(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        request: _HttpRequest,
        headers_sent: list[bool],
    ) -> None:
        match = _THREAD_EVENTS.fullmatch(urlsplit(request.target).path)
        assert match is not None
        query = parse_qs(urlsplit(request.target).query)
        raw_cursor = query.get("after_seq", [request.headers.get("last-event-id", "0")])[0]
        cursor = int(raw_cursor)
        if cursor < 0:
            raise ValueError("after_seq must be non-negative")
        await self._service.ensure_thread(match.group(1))
        raw_tail = query.get("tail", [""])[0]
        if raw_tail:
            tail = int(raw_tail)
            if not (1 <= tail <= 5000):
                raise ValueError("tail must be between 1 and 5000")
            if cursor == 0:
                latest = await self._service.latest_event_seq(match.group(1))
                cursor = max(0, latest - tail)
        writer.write(
            (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/event-stream\r\n"
                f"X-CodeRook-API-Version: {HTTP_API_VERSION}\r\n"
                "Cache-Control: no-cache\r\n"
                "X-Content-Type-Options: nosniff\r\n"
                "Connection: keep-alive\r\n\r\n"
            ).encode("ascii")
        )
        headers_sent[0] = True
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
                wake = asyncio.create_task(
                    self._service.wait_for_change(0.5), name="api-sse-wake"
                )
                await asyncio.wait(
                    {disconnected, wake}, return_when=asyncio.FIRST_COMPLETED
                )
                if not wake.done():
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
        finally:
            disconnected.cancel()
            await asyncio.gather(disconnected, return_exceptions=True)

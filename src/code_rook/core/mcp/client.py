from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

from code_rook.core.processes import ProcessSupervisor, explicit_extension_environment

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


class McpToolError(Exception):
    """MCP server 返回的应用层错误（连接正常，但工具调用失败）"""
    pass


@dataclass
class McpToolDef:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class McpResourceDef:
    uri: str
    name: str
    description: str = ""
    mime_type: str = ""


@dataclass
class McpPromptDef:
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)


# 通过 stdio 或 TCP 与 MCP server 通信的 JSON-RPC 2.0 客户端
class McpClient:
    # 初始化 MCP 连接状态并接入可选 daemon 进程监督器
    def __init__(self, process_supervisor: ProcessSupervisor | None = None) -> None:
        self._id = 0
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._transport = ""
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._http_url = ""
        self._http_session_id = ""
        self._process_supervisor = process_supervisor
        self._server_capabilities: dict[str, Any] = {}
        self._http_stream_task: asyncio.Task[None] | None = None
        self._http_stream_messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=256
        )
        self._http_last_event_id = ""
        self._legacy_sse_task: asyncio.Task[None] | None = None
        self._legacy_sse_ready: asyncio.Future[None] | None = None
        self._legacy_sse_message_url = ""
        self._legacy_sse_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    _STREAM_LIMIT = 64 * 1024 * 1024  # 64 MB，防止大响应触发 LimitOverrunError

    # 启动 stdio 子进程并完成 MCP initialize 握手
    async def connect_stdio(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        merged_env = explicit_extension_environment(env)
        supervisor = self._process_supervisor or ProcessSupervisor()
        self._process_supervisor = supervisor
        self._proc = await supervisor.start_exec(
            command,
            *args,
            label=f"mcp:{command}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            limit=self._STREAM_LIMIT,
        )
        self._reader = self._proc.stdout
        self._writer_proc = self._proc.stdin
        self._transport = "stdio"
        # 后台持续读取 stderr，防止管道缓冲区满导致子进程阻塞
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._initialize()

    # 通过 TCP 连接到 MCP server 并完成 initialize 握手
    async def connect_tcp(self, host: str, port: int) -> None:
        self._reader, tcp_writer = await asyncio.open_connection(
            host, port, limit=self._STREAM_LIMIT
        )
        self._tcp_writer = tcp_writer
        self._transport = "tcp"
        await self._initialize()

    # 连接 MCP Streamable HTTP endpoint 并完成带 session id 的 initialize
    async def connect_streamable_http(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._validate_http_endpoint(url, transport_name="Streamable HTTP")
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-03-26",
                **(headers or {}),
            },
            transport=transport,
        )
        self._http_url = url
        self._transport = "streamable_http"
        await self._initialize()

    # 连接 MCP legacy SSE endpoint，等待 message endpoint 后完成 initialize 握手
    async def connect_sse(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._validate_http_endpoint(url, transport_name="SSE")
        self._http_client = httpx.AsyncClient(
            timeout=30.0,
            headers={"Accept": "text/event-stream", **(headers or {})},
            transport=transport,
        )
        self._http_url = url
        self._transport = "legacy_sse"
        self._legacy_sse_ready = asyncio.get_running_loop().create_future()
        self._legacy_sse_task = asyncio.create_task(
            self._run_legacy_sse(),
            name="mcp-legacy-sse",
        )
        try:
            await asyncio.wait_for(asyncio.shield(self._legacy_sse_ready), timeout=10.0)
            await self._initialize()
        except BaseException:
            await self.close()
            raise

    # 拒绝远端明文 MCP HTTP/SSE endpoint，避免凭据和工具数据裸传
    @staticmethod
    def _validate_http_endpoint(url: str, *, transport_name: str) -> None:
        parsed = httpx.URL(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"MCP {transport_name} URL must use http or https")
        if parsed.scheme == "http" and parsed.host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError(f"remote MCP {transport_name} endpoints require HTTPS")

    # 发送 initialize 请求完成 MCP 握手
    async def _initialize(self) -> None:
        result = await self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "coderook", "version": "0.1"},
        })
        capabilities = result.get("capabilities", {})
        self._server_capabilities = (
            dict(capabilities) if isinstance(capabilities, dict) else {}
        )
        await self._notify("notifications/initialized", {})

    # 返回 initialize 握手声明的服务端能力快照
    @property
    def server_capabilities(self) -> dict[str, Any]:
        return dict(self._server_capabilities)

    # 列出 MCP server 提供的工具定义
    async def list_tools(self) -> list[McpToolDef]:
        response = await self._call("tools/list", {})
        tools = []
        for t in response.get("tools", []):
            tools.append(McpToolDef(
                name=t.get("name", ""),
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            ))
        return tools

    # 通过独立 resources/list 能力列出资源，保持资源不伪装为工具
    async def list_resources(self) -> list[McpResourceDef]:
        response = await self._call("resources/list", {})
        resources: list[McpResourceDef] = []
        for item in response.get("resources", []):
            if not isinstance(item, dict):
                continue
            resources.append(
                McpResourceDef(
                    uri=str(item.get("uri", "")),
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    mime_type=str(item.get("mimeType", "")),
                )
            )
        return resources

    # 读取指定 MCP resource 的原生 contents 块
    async def read_resource(self, uri: str) -> list[dict[str, Any]]:
        response = await self._call("resources/read", {"uri": uri})
        contents = response.get("contents", [])
        return [dict(item) for item in contents if isinstance(item, dict)]

    # 通过独立 prompts/list 能力列出 prompt 模板
    async def list_prompts(self) -> list[McpPromptDef]:
        response = await self._call("prompts/list", {})
        prompts: list[McpPromptDef] = []
        for item in response.get("prompts", []):
            if not isinstance(item, dict):
                continue
            arguments = item.get("arguments", [])
            prompts.append(
                McpPromptDef(
                    name=str(item.get("name", "")),
                    description=str(item.get("description", "")),
                    arguments=[
                        dict(argument)
                        for argument in arguments
                        if isinstance(argument, dict)
                    ] if isinstance(arguments, list) else [],
                )
            )
        return prompts

    # 获取 MCP prompt 渲染后的原生 messages 块
    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._call(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
        )
        return dict(response)

    # 调用 MCP 工具并拼接 text 内容；连接异常和工具错误由专用异常表示
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        response = await self._call("tools/call", {"name": name, "arguments": arguments})
        parts: list[str] = []
        for item in response.get("content", []):
            if item.get("type") == "text":
                parts.append(str(item["text"]))
        return "\n".join(parts)

    # 启动 Streamable HTTP GET 服务端消息流并在断线后按 Last-Event-ID 有界重连
    def start_server_stream(self, *, max_reconnects: int = 3) -> None:
        if self._transport != "streamable_http":
            raise McpServerUnavailableError("server stream requires Streamable HTTP")
        if max_reconnects < 0:
            raise ValueError("max_reconnects must not be negative")
        if self._http_stream_task is not None and not self._http_stream_task.done():
            return
        self._http_stream_task = asyncio.create_task(
            self._run_http_server_stream(max_reconnects=max_reconnects),
            name="mcp-http-server-stream",
        )

    # 读取一个服务端主动消息，调用方可用 timeout 明确限制等待
    async def next_server_message(
        self,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if timeout is None:
            return await self._http_stream_messages.get()
        return await asyncio.wait_for(
            self._http_stream_messages.get(),
            timeout=timeout,
        )

    # 消费单个或重连后的 GET SSE 响应，并记录最近事件游标
    async def _run_http_server_stream(self, *, max_reconnects: int) -> None:
        if self._http_client is None or not self._http_url:
            return
        reconnects = 0
        while True:
            headers = {
                "Accept": "text/event-stream",
                **(
                    {"Mcp-Session-Id": self._http_session_id}
                    if self._http_session_id
                    else {}
                ),
                **(
                    {"Last-Event-ID": self._http_last_event_id}
                    if self._http_last_event_id
                    else {}
                ),
            }
            try:
                async with self._http_client.stream(
                    "GET",
                    self._http_url,
                    headers=headers,
                ) as response:
                    if response.status_code in {404, 405}:
                        return
                    response.raise_for_status()
                    event_id = ""
                    async for line in response.aiter_lines():
                        if line.startswith("id:"):
                            event_id = line.removeprefix("id:").strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        try:
                            payload = json.loads(line.removeprefix("data:").strip())
                        except json.JSONDecodeError:
                            continue
                        if isinstance(payload, dict):
                            if event_id:
                                self._http_last_event_id = event_id
                            if self._http_stream_messages.full():
                                self._http_stream_messages.get_nowait()
                            self._http_stream_messages.put_nowait(payload)
            except asyncio.CancelledError:
                raise
            except httpx.HTTPError:
                log.debug("mcp HTTP server stream disconnected", exc_info=True)
            if reconnects >= max_reconnects:
                return
            reconnects += 1
            await asyncio.sleep(min(0.1 * (2 ** (reconnects - 1)), 1.0))

    # 持续消费 legacy SSE endpoint 与 JSON-RPC message 事件并分派到等待请求
    async def _run_legacy_sse(self) -> None:
        if self._http_client is None or not self._http_url:
            return
        event_type = ""
        data_lines: list[str] = []
        failure: BaseException | None = None
        try:
            async with self._http_client.stream("GET", self._http_url) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line.removeprefix("event:").strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.removeprefix("data:").lstrip())
                    elif not line:
                        if data_lines:
                            self._dispatch_legacy_sse_event(
                                event_type or "message",
                                "\n".join(data_lines),
                            )
                        event_type = ""
                        data_lines = []
                if data_lines:
                    self._dispatch_legacy_sse_event(
                        event_type or "message",
                        "\n".join(data_lines),
                    )
            failure = McpServerUnavailableError("MCP SSE stream closed")
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            failure = McpServerUnavailableError(
                f"MCP SSE stream failed: {type(exc).__name__}"
            )
        finally:
            if failure is not None:
                ready = self._legacy_sse_ready
                if ready is not None and not ready.done():
                    ready.set_exception(failure)
                for pending in list(self._legacy_sse_pending.values()):
                    if not pending.done():
                        pending.set_exception(failure)

    # 处理 legacy SSE endpoint 或 message 事件并拒绝跨源回传地址
    def _dispatch_legacy_sse_event(self, event_type: str, data: str) -> None:
        if event_type == "endpoint":
            base = httpx.URL(self._http_url)
            endpoint = httpx.URL(urljoin(self._http_url, data.strip()))
            if (
                endpoint.scheme != base.scheme
                or endpoint.host != base.host
                or endpoint.port != base.port
            ):
                ready = self._legacy_sse_ready
                if ready is not None and not ready.done():
                    ready.set_exception(
                        McpServerUnavailableError(
                            "MCP SSE message endpoint must use the stream origin"
                        )
                    )
                return
            self._legacy_sse_message_url = str(endpoint)
            ready = self._legacy_sse_ready
            if ready is not None and not ready.done():
                ready.set_result(None)
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            log.debug("mcp SSE ignored malformed event")
            return
        if not isinstance(payload, dict):
            return
        message_id = payload.get("id")
        pending = self._legacy_sse_pending.get(str(message_id))
        if message_id is not None and pending is not None and not pending.done():
            pending.set_result(payload)
            return
        if self._http_stream_messages.full():
            self._http_stream_messages.get_nowait()
        self._http_stream_messages.put_nowait(payload)

    # 向 legacy SSE 公布的 message endpoint 发送请求或通知
    async def _legacy_sse_post(self, payload: dict[str, Any]) -> None:
        if self._http_client is None or not self._legacy_sse_message_url:
            raise McpServerUnavailableError("MCP SSE message endpoint unavailable")
        if self._legacy_sse_task is None or self._legacy_sse_task.done():
            raise McpServerUnavailableError("MCP SSE stream is not connected")
        try:
            response = await self._http_client.post(
                self._legacy_sse_message_url,
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise McpServerUnavailableError(
                f"MCP SSE request failed: {type(exc).__name__}"
            ) from exc

    # 通过 legacy SSE POST 请求并等待 GET stream 上匹配的 JSON-RPC 响应
    async def _legacy_sse_request(
        self,
        request: dict[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        pending: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._legacy_sse_pending[request_id] = pending
        try:
            await self._legacy_sse_post(request)
            message = await asyncio.wait_for(pending, timeout=30.0)
        except TimeoutError as exc:
            raise McpServerUnavailableError("MCP SSE response timeout") from exc
        finally:
            self._legacy_sse_pending.pop(request_id, None)
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                raise McpToolError(
                    f"{error.get('message', str(error))} "
                    f"(code={error.get('code')})"
                )
            raise McpToolError(str(error))
        result = message.get("result", {})
        return dict(result) if isinstance(result, dict) else {}

    # 后台任务：持续读取 stderr 并记录日志，防止管道缓冲区满
    async def _drain_stderr(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                stderr_line = line.decode(errors="replace").rstrip()
                if stderr_line:
                    log.debug("mcp stderr: %s", stderr_line)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("mcp stderr drain stopped", exc_info=True)

    # 关闭连接并终止 stdio 子进程
    async def close(self) -> None:
        if self._legacy_sse_task is not None:
            self._legacy_sse_task.cancel()
            try:
                await self._legacy_sse_task
            except asyncio.CancelledError:
                pass
            self._legacy_sse_task = None
        for pending in list(self._legacy_sse_pending.values()):
            if not pending.done():
                pending.cancel()
        self._legacy_sse_pending.clear()
        if self._http_stream_task is not None:
            self._http_stream_task.cancel()
            try:
                await self._http_stream_task
            except asyncio.CancelledError:
                pass
            self._http_stream_task = None
        # 先取消 stderr 读取任务
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None
        if self._transport == "stdio" and self._proc is not None:
            assert self._process_supervisor is not None
            await self._process_supervisor.terminate(self._proc)
        elif self._transport == "tcp":
            writer = getattr(self, "_tcp_writer", None)
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        elif self._transport in {"streamable_http", "legacy_sse"} and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # 发送 JSON-RPC 请求并等待响应；id 比较用字符串兼容服务端返回字符串 id 的情况
    async def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        req_id = self._id
        req_id_str = str(req_id)
        request = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        try:
            async with self._lock:
                if self._transport == "streamable_http":
                    return await self._http_request(request, req_id_str)
                if self._transport == "legacy_sse":
                    return await self._legacy_sse_request(request, req_id_str)
                await self._write_line(json.dumps(request))
                while True:
                    line = await self._read_line()
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug("mcp: ignoring non-JSON line: %r", line[:200])
                        continue
                    msg_id = msg.get("id")
                    if msg_id is None:
                        # server-initiated notification，忽略
                        log.debug("mcp: received server notification: %s", msg.get("method"))
                        continue
                    if str(msg_id) == req_id_str:
                        if "error" in msg:
                            err = msg["error"]
                            raise McpToolError(
                                f"{err.get('message', str(err))} (code={err.get('code')})"
                            )
                        result: dict[str, Any] = msg.get("result", {})
                        return result
        except asyncio.CancelledError:
            await asyncio.shield(self._send_cancellation(req_id, method))
            raise

    # 在本地请求被取消时向 MCP server 发送标准取消通知
    async def _send_cancellation(self, request_id: int, method: str) -> None:
        notification = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {
                "requestId": request_id,
                "reason": f"CodeRook cancelled {method}",
            },
        }
        try:
            if self._transport == "streamable_http":
                await self._http_request(notification, None)
            elif self._transport == "legacy_sse":
                await self._legacy_sse_post(notification)
            else:
                await self._write_line(json.dumps(notification))
        except (McpServerUnavailableError, McpToolError, OSError):
            log.debug("mcp cancellation notification failed", exc_info=True)

    # 发送 JSON-RPC 通知（无响应）
    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        notification = {"jsonrpc": "2.0", "method": method, "params": params}
        if self._transport == "streamable_http":
            await self._http_request(notification, None)
            return
        if self._transport == "legacy_sse":
            await self._legacy_sse_post(notification)
            return
        await self._write_line(json.dumps(notification))

    # 通过单一 HTTP endpoint 发送 JSON-RPC，并解析 JSON 或 SSE data 响应
    async def _http_request(
        self,
        request: dict[str, Any],
        request_id: str | None,
    ) -> dict[str, Any]:
        if self._http_client is None or not self._http_url:
            raise McpServerUnavailableError("Streamable HTTP client unavailable")
        headers = (
            {"Mcp-Session-Id": self._http_session_id}
            if self._http_session_id
            else {}
        )
        try:
            response = await self._http_client.post(
                self._http_url,
                headers=headers,
                json=request,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise McpServerUnavailableError(
                f"MCP Streamable HTTP request failed: {type(exc).__name__}"
            ) from exc
        session_id = response.headers.get("Mcp-Session-Id", "")
        if session_id:
            self._http_session_id = session_id
        if response.status_code == 202 or not response.content:
            return {}
        content_type = response.headers.get("content-type", "").casefold()
        messages: list[dict[str, Any]] = []
        if "text/event-stream" in content_type:
            for line in response.text.splitlines():
                if not line.startswith("data:"):
                    continue
                try:
                    payload = json.loads(line.removeprefix("data:").strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    messages.append(payload)
        else:
            payload = response.json()
            if isinstance(payload, dict):
                messages.append(payload)
        for message in messages:
            if request_id is not None and str(message.get("id")) != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    raise McpToolError(
                        f"{error.get('message', str(error))} "
                        f"(code={error.get('code')})"
                    )
                raise McpToolError(str(error))
            result = message.get("result", {})
            return dict(result) if isinstance(result, dict) else {}
        if request_id is not None:
            raise McpServerUnavailableError("MCP HTTP response omitted matching request id")
        return {}

    # 向 MCP server 写入一行 JSON
    async def _write_line(self, line: str) -> None:
        data = (line + "\n").encode()
        if self._transport == "stdio":
            w = self._proc.stdin if self._proc else None
            if w is None:
                raise McpServerUnavailableError("stdio writer unavailable")
            w.write(data)
            await w.drain()
        elif self._transport == "tcp":
            w = getattr(self, "_tcp_writer", None)
            if w is None:
                raise McpServerUnavailableError("tcp writer unavailable")
            w.write(data)
            await w.drain()

    # 从 MCP server 读取一行 JSON；跳过空行，仅 EOF（b""）才视为连接断开
    async def _read_line(self) -> str:
        if self._reader is None:
            raise McpServerUnavailableError("reader unavailable")
        while True:
            try:
                data = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
            except TimeoutError:
                raise McpServerUnavailableError("MCP server read timeout")
            except asyncio.LimitOverrunError as exc:
                raise McpServerUnavailableError(
                    f"MCP response too large (>{self._STREAM_LIMIT // 1024 // 1024}MB): {exc}"
                ) from exc
            if data == b"":
                raise McpServerUnavailableError("MCP server closed connection")
            line = data.decode(errors="replace").strip()
            if line:
                return line

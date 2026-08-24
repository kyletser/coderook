from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.llm.credentials import CredentialResolution
from code_rook.core.llm.routes import (
    CredentialSource,
    DoctorCapabilityReceipt,
    DoctorCheckStatus,
    ProviderRoute,
    RouteDoctorReceipt,
)

DoctorCategory = Literal[
    "ok",
    "credential",
    "tls",
    "schema",
    "model",
    "network",
    "streaming",
    "termination",
    "capability",
]
DoctorReadiness = Literal["basic", "verified", "failed"]
ProbeKind = Literal["completion", "tool", "parallel_tools", "image"]
TerminalKind = Literal[
    "normal",
    "tool",
    "length",
    "content_filtered",
    "failed",
    "incomplete",
    "cancelled",
    "unknown",
]

_MAX_STREAM_BYTES = 512 * 1024
_MAX_STREAM_EVENTS = 512
_TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)
_TOOL_ONE = "coderook_doctor_echo_one"
_TOOL_TWO = "coderook_doctor_echo_two"


class ProviderDoctorCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: DoctorCheckStatus
    message: str


class ProviderDoctorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "error"]
    category: DoctorCategory
    route_id: str
    message: str
    credential_source: CredentialSource
    http_status: int | None = None
    readiness: DoctorReadiness = "failed"
    route_digest: str = ""
    checked_at: str = ""
    basic: ProviderDoctorCheck = ProviderDoctorCheck(
        status="not_run",
        message="provider probe was not run",
    )
    capabilities: dict[str, ProviderDoctorCheck] = Field(default_factory=dict)

    # 将全部必需探针通过且摘要匹配的结果转换成脱敏持久收据
    def to_receipt(self, route: ProviderRoute) -> RouteDoctorReceipt:
        expected = route.validation_digest()
        if (
            self.status != "ok"
            or self.readiness != "verified"
            or self.basic.status != "passed"
            or self.route_id != route.id
            or self.route_digest != expected
            or any(
                name not in self.capabilities or self.capabilities[name].status != "passed"
                for name in _required_capabilities(route)
            )
        ):
            raise ValueError("doctor result is not valid for this route and model")
        return RouteDoctorReceipt(
            route_digest=expected,
            checked_at=self.checked_at,
            basic=DoctorCapabilityReceipt.model_validate(self.basic.model_dump()),
            capabilities={
                name: DoctorCapabilityReceipt.model_validate(check.model_dump())
                for name, check in self.capabilities.items()
            },
        )


@dataclass(frozen=True)
class _Observation:
    status: Literal["ok", "error"]
    category: DoctorCategory
    message: str
    http_status: int | None = None
    streamed: bool = False
    terminal: TerminalKind = "unknown"
    terminal_received: bool = False
    tool_names: tuple[str, ...] = ()


# 返回当前 route 声明后必须真实通过的能力名称
def _required_capabilities(route: ProviderRoute) -> tuple[str, ...]:
    names = ["streaming", "termination"]
    if route.supports_tools:
        names.append("tool_calling")
    if route.supports_parallel_tools:
        names.append("parallel_tools")
    if route.supports_images:
        names.append("images")
    return tuple(names)


# 返回未执行探针与显式不支持能力的诚实初始状态
def _capability_checks(route: ProviderRoute) -> dict[str, ProviderDoctorCheck]:
    return {
        "streaming": ProviderDoctorCheck(
            status="not_run", message="bounded streaming probe was not run"
        ),
        "termination": ProviderDoctorCheck(
            status="not_run", message="normal termination was not observed"
        ),
        "tool_calling": ProviderDoctorCheck(
            status="not_run" if route.supports_tools else "unsupported",
            message=(
                "declared tool calling was not probed"
                if route.supports_tools
                else "the route declares tool calling unsupported"
            ),
        ),
        "parallel_tools": ProviderDoctorCheck(
            status="not_run" if route.supports_parallel_tools else "unsupported",
            message=(
                "declared parallel tools were not probed"
                if route.supports_parallel_tools
                else "the route declares parallel tools unsupported"
            ),
        ),
        "images": ProviderDoctorCheck(
            status="not_run" if route.supports_images else "unsupported",
            message=(
                "declared image input was not probed"
                if route.supports_images
                else "the route declares image input unsupported"
            ),
        ),
    }


# 构造绑定 route/model 摘要且不含凭据和响应正文的 Doctor 结果
def _result(
    route: ProviderRoute,
    credential_source: CredentialSource,
    *,
    status: Literal["ok", "error"],
    category: DoctorCategory,
    message: str,
    basic: ProviderDoctorCheck,
    capabilities: dict[str, ProviderDoctorCheck],
    http_status: int | None = None,
) -> ProviderDoctorResult:
    return ProviderDoctorResult(
        status=status,
        category=category,
        route_id=route.id,
        message=message,
        credential_source=credential_source,
        http_status=http_status,
        readiness="verified" if status == "ok" else "failed",
        route_digest=route.validation_digest(),
        checked_at=datetime.now(UTC).isoformat(),
        basic=basic,
        capabilities=capabilities,
    )


# 判断异常链是否表明 TLS 握手或证书校验失败
def _is_tls_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        text = f"{type(current).__name__} {current}".casefold()
        if any(marker in text for marker in ("ssl", "tls", "certificate")):
            return True
        current = current.__cause__ or current.__context__
    return False


# 将 Anthropic API 根地址规范为 Messages endpoint
def _endpoint(route: ProviderRoute) -> str:
    raw = str(route.base_url)
    if route.wire_format != "anthropic_messages":
        return raw
    parsed = urlsplit(raw)
    path = parsed.path.rstrip("/")
    if path.endswith("/messages"):
        return raw.rstrip("/")
    if path.endswith("/v1"):
        target = f"{path}/messages"
    else:
        target = f"{path}/v1/messages" if path else "/v1/messages"
    return urlunsplit((parsed.scheme, parsed.netloc, target, parsed.query, ""))


# 构造单个 Doctor 函数的最小 JSON schema
def _tool_schema(name: str) -> dict[str, object]:
    return {
        "name": name,
        "description": "Return the supplied Doctor probe value.",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }


# 将统一函数 schema 转换成 route wire format 的工具声明
def _wire_tools(route: ProviderRoute, names: tuple[str, ...]) -> list[dict[str, object]]:
    schemas = [_tool_schema(name) for name in names]
    if route.wire_format == "anthropic_messages":
        return schemas
    if route.wire_format == "openai_responses":
        return [
            {
                "type": "function",
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["input_schema"],
                "strict": True,
            }
            for schema in schemas
        ]
    return [
        {
            "type": "function",
            "function": {
                "name": schema["name"],
                "description": schema["description"],
                "parameters": schema["input_schema"],
            },
        }
        for schema in schemas
    ]


# 构造三种协议的有界流式探针请求，不包含用户数据
def _request(
    route: ProviderRoute,
    credential: str,
    kind: ProbeKind,
) -> tuple[dict[str, str], dict[str, object], tuple[str, ...]]:
    headers = {"Content-Type": "application/json"}
    if route.wire_format == "anthropic_messages":
        if credential:
            headers["x-api-key"] = credential
        headers["anthropic-version"] = "2023-06-01"
    elif credential:
        headers["Authorization"] = f"Bearer {credential}"

    prompt = "Reply with exactly OK."
    expected: tuple[str, ...] = ()
    if kind == "tool":
        expected = (_TOOL_ONE,)
    elif kind == "parallel_tools":
        expected = (_TOOL_ONE, _TOOL_TWO)
    if expected:
        prompt = (
            f"Call {', '.join(expected)} exactly once with value doctor in one response. "
            "Do not answer text."
        )

    if route.wire_format == "anthropic_messages":
        content: object = prompt
        if kind == "image":
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": _TINY_PNG,
                    },
                },
                {"type": "text", "text": prompt},
            ]
        payload: dict[str, object] = {
            "model": route.model,
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": content}],
        }
        if expected:
            payload["tools"] = _wire_tools(route, expected)
            payload["tool_choice"] = {
                "type": "any",
                "disable_parallel_tool_use": kind != "parallel_tools",
            }
        return headers, payload, expected

    if route.wire_format == "openai_responses":
        input_value: object = prompt
        if kind == "image":
            input_value = [
                {"role": "user", "content": [
                    {"type": "input_text", "text": prompt},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{_TINY_PNG}",
                    },
                ]}
            ]
        payload = {
            "model": route.model,
            "input": input_value,
            "max_output_tokens": 16,
            "store": False,
            "stream": True,
        }
        if expected:
            payload["tools"] = _wire_tools(route, expected)
            payload["tool_choice"] = "required"
            payload["parallel_tool_calls"] = kind == "parallel_tools"
        return headers, payload, expected

    user_content: object = prompt
    if kind == "image":
        user_content = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"},
            },
        ]
    payload = {
        "model": route.model,
        "messages": [{"role": "user", "content": user_content}],
        "stream": True,
        ("max_completion_tokens" if route.provider == "openai" else "max_tokens"): 16,
    }
    if expected:
        payload["tools"] = _wire_tools(route, expected)
        payload["tool_choice"] = "required"
        payload["parallel_tool_calls"] = kind == "parallel_tools"
    return headers, payload, expected


# 将各协议终止原因归一化为 Doctor 内部状态
def _terminal(reason: object) -> TerminalKind:
    normalized = str(reason or "").strip().casefold()
    if normalized in {"stop", "end_turn", "stop_sequence", "completed"}:
        return "normal"
    if normalized in {"tool_calls", "function_call", "tool_use"}:
        return "tool"
    if normalized in {"length", "max_tokens", "max_output_tokens"}:
        return "length"
    if normalized in {"content_filter", "content_filtered", "refusal"}:
        return "content_filtered"
    if normalized in {"failed", "error"}:
        return "failed"
    if normalized in {"cancelled", "canceled"}:
        return "cancelled"
    if normalized in {"incomplete", "in_progress", "queued"}:
        return "incomplete"
    return "unknown"


# 从完整响应提取函数名，忽略所有响应正文和参数
def _full_tools(route: ProviderRoute, payload: dict[str, object]) -> tuple[str, ...]:
    blocks: object
    if route.wire_format == "openai_chat":
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else {}
        message = first.get("message") if isinstance(first, dict) else {}
        blocks = message.get("tool_calls") if isinstance(message, dict) else []
        names = []
        for block in blocks if isinstance(blocks, list) else []:
            function = block.get("function") if isinstance(block, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if isinstance(name, str):
                names.append(name)
        return tuple(names)
    blocks = payload.get("output" if route.wire_format == "openai_responses" else "content")
    expected_type = "function_call" if route.wire_format == "openai_responses" else "tool_use"
    return tuple(
        str(block["name"])
        for block in (blocks if isinstance(blocks, list) else [])
        if isinstance(block, dict)
        and block.get("type") == expected_type
        and isinstance(block.get("name"), str)
    )


# 解析非 SSE 响应以区分基础 wire schema 通过与流式能力失败
def _full_observation(route: ProviderRoute, payload: object, status: int) -> _Observation:
    if not isinstance(payload, dict):
        return _Observation(
            "error", "schema", "provider returned an incompatible response schema", status
        )
    names = _full_tools(route, payload)
    if route.wire_format == "openai_chat":
        choices = payload.get("choices")
        valid = isinstance(choices, list)
        first = choices[0] if valid and choices else {}
        terminal = _terminal(first.get("finish_reason") if isinstance(first, dict) else None)
    elif route.wire_format == "openai_responses":
        valid = isinstance(payload.get("output"), list)
        terminal = _terminal(payload.get("status"))
    else:
        valid = isinstance(payload.get("content"), list)
        terminal = _terminal(payload.get("stop_reason"))
    if names and terminal == "normal":
        terminal = "tool"
    if not valid:
        return _Observation(
            "error", "schema", "provider returned an incompatible response schema", status
        )
    return _Observation(
        "ok",
        "ok",
        "provider accepted the bounded request",
        status,
        False,
        terminal,
        terminal != "unknown",
        names,
    )


# 有界消费 SSE，只保留协议状态和函数名而不保留响应正文
async def _stream_observation(
    route: ProviderRoute,
    response: httpx.Response,
) -> _Observation:
    recognized = False
    transport_done = False
    terminal_received = False
    terminal: TerminalKind = "unknown"
    total_bytes = 0
    events = 0
    names: list[str] = []
    indexed_names: dict[int, str] = {}
    async for line in response.aiter_lines():
        total_bytes += len(line.encode("utf-8", errors="replace"))
        if total_bytes > _MAX_STREAM_BYTES or events >= _MAX_STREAM_EVENTS:
            return _Observation(
                "error",
                "schema",
                "provider stream exceeded the bounded Doctor limit",
                response.status_code,
                True,
            )
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        raw = stripped[5:].strip()
        if raw == "[DONE]":
            transport_done = True
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        events += 1
        if route.wire_format == "openai_chat":
            choices = event.get("choices")
            if not isinstance(choices, list):
                continue
            recognized = True
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                candidate = _terminal(choice.get("finish_reason"))
                if candidate != "unknown":
                    terminal = candidate
                delta = choice.get("delta")
                calls = delta.get("tool_calls") if isinstance(delta, dict) else None
                for call in calls if isinstance(calls, list) else []:
                    if not isinstance(call, dict):
                        continue
                    raw_index = call.get("index")
                    index = raw_index if isinstance(raw_index, int) else 0
                    function = call.get("function")
                    name = function.get("name") if isinstance(function, dict) else None
                    if isinstance(name, str) and name:
                        indexed_names[index] = name
        elif route.wire_format == "openai_responses":
            event_type = event.get("type")
            if not isinstance(event_type, str) or not event_type.startswith("response."):
                continue
            recognized = True
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                name = item.get("name")
                if isinstance(name, str):
                    names.append(name)
            if event_type in {
                "response.completed",
                "response.incomplete",
                "response.failed",
                "response.cancelled",
            }:
                terminal_received = True
                full = event.get("response")
                if isinstance(full, dict):
                    names.extend(_full_tools(route, full))
                    terminal = _terminal(full.get("status"))
                else:
                    terminal = _terminal(event_type.removeprefix("response."))
                if names and terminal == "normal":
                    terminal = "tool"
        else:
            event_type = event.get("type")
            if not isinstance(event_type, str):
                continue
            if not event_type.startswith(("message_", "content_block_", "ping", "error")):
                continue
            recognized = True
            if event_type == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str):
                        names.append(name)
            elif event_type == "message_delta":
                delta = event.get("delta")
                candidate = _terminal(
                    delta.get("stop_reason") if isinstance(delta, dict) else None
                )
                if candidate != "unknown":
                    terminal = candidate
            elif event_type == "message_stop":
                terminal_received = True
            elif event_type == "error":
                terminal = "failed"
                terminal_received = True
    if route.wire_format == "openai_chat":
        names = [indexed_names[index] for index in sorted(indexed_names)]
        terminal_received = transport_done and terminal != "unknown"
    elif route.wire_format == "anthropic_messages" and terminal_received:
        terminal_received = terminal != "unknown"
    if not recognized:
        return _Observation(
            "error",
            "schema",
            "provider returned an incompatible event stream",
            response.status_code,
            True,
        )
    return _Observation(
        "ok",
        "ok",
        "provider returned a bounded event stream",
        response.status_code,
        True,
        terminal,
        terminal_received,
        tuple(names),
    )


# 将 HTTP 错误分类为 credential、model 或 schema，不保留响应正文
def _http_failure(status: int, body: bytes) -> _Observation:
    text = body[:64_000].decode("utf-8", errors="replace").casefold()
    if status in {401, 403}:
        category: DoctorCategory = "credential"
        message = "credential was rejected by the provider"
    elif "model" in text and any(
        marker in text for marker in ("not found", "unknown", "invalid", "does not exist")
    ):
        category = "model"
        message = "configured model was rejected by the provider"
    else:
        category = "schema"
        message = "provider rejected the route request schema or endpoint"
    return _Observation("error", category, message, status)


# 有界读取非流式或错误响应，避免 Doctor 被超大正文占满内存
async def _read_bounded(
    response: httpx.Response,
    limit: int = _MAX_STREAM_BYTES,
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        remaining = limit - size
        if remaining <= 0:
            return b"".join(chunks), True
        chunks.append(chunk[:remaining])
        size += min(len(chunk), remaining)
        if len(chunk) > remaining:
            return b"".join(chunks), True
    return b"".join(chunks), False


class ProviderDoctor:
    # 初始化可注入 HTTP client 与单探针超时的诊断器
    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 20.0,
    ) -> None:
        self._client = client
        self._timeout_s = timeout_s

    # 发起单个有界流式请求并只返回脱敏协议观察值
    async def _probe(
        self,
        client: httpx.AsyncClient,
        route: ProviderRoute,
        credential: str,
        kind: ProbeKind,
    ) -> tuple[_Observation, tuple[str, ...]]:
        headers, payload, expected = _request(route, credential, kind)
        try:
            async with asyncio.timeout(self._timeout_s):
                async with client.stream(
                    "POST", _endpoint(route), headers=headers, json=payload
                ) as response:
                    if response.is_error:
                        raw, _ = await _read_bounded(response, 64_000)
                        return _http_failure(response.status_code, raw), expected
                    if "text/event-stream" in response.headers.get(
                        "content-type", ""
                    ).casefold():
                        return await _stream_observation(route, response), expected
                    raw, overflow = await _read_bounded(response)
                    if overflow:
                        return _Observation(
                            "error",
                            "schema",
                            "provider response exceeded the bounded Doctor limit",
                            response.status_code,
                        ), expected
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        parsed = None
                    return _full_observation(route, parsed, response.status_code), expected
        except (httpx.HTTPError, TimeoutError) as exc:
            category: DoctorCategory = "tls" if _is_tls_error(exc) else "network"
            message = (
                "TLS handshake or certificate validation failed"
                if category == "tls"
                else "provider endpoint could not be reached within the Doctor limit"
            )
            return _Observation("error", category, message), expected

    # 用同一连接最多执行三次探针并要求全部声明能力通过
    async def _check_with_client(
        self,
        client: httpx.AsyncClient,
        route: ProviderRoute,
        credential: CredentialResolution,
    ) -> ProviderDoctorResult:
        capabilities = _capability_checks(route)
        basic = ProviderDoctorCheck(status="not_run", message="provider probe was not run")
        primary, _ = await self._probe(client, route, credential.value or "", "completion")
        if primary.status != "ok":
            basic = ProviderDoctorCheck(status="failed", message=primary.message)
            return _result(
                route,
                credential.source,
                status="error",
                category=primary.category,
                message=primary.message,
                basic=basic,
                capabilities=capabilities,
                http_status=primary.http_status,
            )
        basic = ProviderDoctorCheck(
            status="passed", message="provider accepted the bounded route/model request"
        )
        capabilities["streaming"] = ProviderDoctorCheck(
            status="passed" if primary.streamed else "failed",
            message=(
                "wire-format event stream was observed"
                if primary.streamed
                else "provider returned a non-streaming response"
            ),
        )
        normal = primary.streamed and primary.terminal_received and primary.terminal == "normal"
        capabilities["termination"] = ProviderDoctorCheck(
            status="passed" if normal else "failed",
            message=(
                "normal completed terminal state was observed"
                if normal
                else "normal completed terminal state was not observed"
            ),
        )
        if not primary.streamed or not normal:
            category: DoctorCategory = "streaming" if not primary.streamed else "termination"
            return _result(
                route,
                credential.source,
                status="error",
                category=category,
                message=(
                    "provider did not return the required event stream"
                    if category == "streaming"
                    else "provider stream did not end with a normal completed state"
                ),
                basic=basic,
                capabilities=capabilities,
                http_status=primary.http_status,
            )

        if route.supports_tools:
            kind: ProbeKind = "parallel_tools" if route.supports_parallel_tools else "tool"
            observed, expected = await self._probe(
                client, route, credential.value or "", kind
            )
            actual = tuple(dict.fromkeys(observed.tool_names))
            tool_ok = (
                observed.status == "ok"
                and observed.streamed
                and observed.terminal_received
                and observed.terminal == "tool"
                and set(actual) == set(expected)
                and len(actual) == len(expected)
            )
            capabilities["tool_calling"] = ProviderDoctorCheck(
                status="passed" if tool_ok else "failed",
                message=(
                    "declared tool call completed in a terminal stream"
                    if tool_ok
                    else "declared tool call was not observed as a complete terminal response"
                ),
            )
            if route.supports_parallel_tools:
                capabilities["parallel_tools"] = ProviderDoctorCheck(
                    status="passed" if tool_ok else "failed",
                    message=(
                        "two distinct tool calls completed in one response"
                        if tool_ok
                        else "two distinct complete tool calls were not observed in one response"
                    ),
                )
            if not tool_ok:
                return _result(
                    route,
                    credential.source,
                    status="error",
                    category=observed.category if observed.status == "error" else "capability",
                    message="declared tool capability probe failed",
                    basic=basic,
                    capabilities=capabilities,
                    http_status=observed.http_status,
                )

        if route.supports_images:
            observed, _ = await self._probe(
                client, route, credential.value or "", "image"
            )
            image_ok = (
                observed.status == "ok"
                and observed.streamed
                and observed.terminal_received
                and observed.terminal == "normal"
            )
            capabilities["images"] = ProviderDoctorCheck(
                status="passed" if image_ok else "failed",
                message=(
                    "tiny in-memory image input completed normally"
                    if image_ok
                    else "declared image input did not complete normally"
                ),
            )
            if not image_ok:
                return _result(
                    route,
                    credential.source,
                    status="error",
                    category=observed.category if observed.status == "error" else "capability",
                    message="declared image capability probe failed",
                    basic=basic,
                    capabilities=capabilities,
                    http_status=observed.http_status,
                )

        return _result(
            route,
            credential.source,
            status="ok",
            category="ok",
            message="all required route capabilities passed bounded live probes",
            basic=basic,
            capabilities=capabilities,
            http_status=primary.http_status,
        )

    # 探测 route 并返回不含密钥、响应正文和异常细节的分类结果
    async def check(
        self,
        route: ProviderRoute,
        credential: CredentialResolution,
    ) -> ProviderDoctorResult:
        if credential.value is None and route.credential_required:
            return _result(
                route,
                "missing",
                status="error",
                category="credential",
                message="credential is missing",
                basic=ProviderDoctorCheck(status="failed", message="credential is missing"),
                capabilities=_capability_checks(route),
            )
        if self._client is not None:
            return await self._check_with_client(self._client, route, credential)
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            return await self._check_with_client(client, route, credential)

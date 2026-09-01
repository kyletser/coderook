from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from types import UnionType
from typing import Any, Union, get_args, get_origin

import httpx
from pydantic import BaseModel

from code_rook.core.authority import RuntimeMode
from code_rook.core.receipts.models import TurnReceipt
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    ThreadRecord,
    TurnItemRecord,
    TurnRecord,
)


# 删除公共响应模型及其嵌套模型尚未识别的增量字段
def _filter_public_payload(model: type[BaseModel], payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {
        name: _filter_public_value(field.annotation, payload[name])
        for name, field in model.model_fields.items()
        if name in payload
    }


# 按字段类型递归处理嵌套公共模型和容器
def _filter_public_value(annotation: Any, value: Any) -> Any:
    origin = get_origin(annotation)
    if origin in {list, tuple, set, frozenset}:
        args = get_args(annotation)
        if args and isinstance(value, (list, tuple, set, frozenset)):
            return [_filter_public_value(args[0], item) for item in value]
        return value
    if origin in {UnionType, Union}:
        for candidate in get_args(annotation):
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                return _filter_public_payload(candidate, value)
        return value
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _filter_public_payload(annotation, value)
    return value


# 以向前兼容方式校验单个公共 API 响应模型
def _validate_public_model[ModelT: BaseModel](
    model: type[ModelT], payload: Any
) -> ModelT:
    return model.model_validate(_filter_public_payload(model, payload))


# 以向前兼容方式校验公共 API 响应模型列表
def _validate_public_list[ModelT: BaseModel](
    model: type[ModelT], payload: Any
) -> list[ModelT]:
    if not isinstance(payload, list):
        raise ValueError("public API list response must be an array")
    return [_validate_public_model(model, item) for item in payload]


class SdkError(RuntimeError):
    # 保存 HTTP 状态和服务端脱敏错误文本
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"CodeRook API error {status_code}: {message}")
        self.status_code = status_code


# 从 JSON 错误响应中提取稳定消息
def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return response.text[:500]
    if isinstance(payload, dict):
        return str(payload.get("error", response.reason_phrase))
    return response.reason_phrase


class AsyncCodeRookClient:
    # 初始化异步 HTTP/SSE 客户端，Bearer token 只进入请求头
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    # 关闭底层连接池
    async def close(self) -> None:
        await self._client.aclose()

    # 进入异步上下文
    async def __aenter__(self) -> AsyncCodeRookClient:
        return self

    # 离开异步上下文并关闭连接
    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        await self.close()

    # 执行 JSON 请求并统一转换非 2xx 错误
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        if not response.is_success:
            raise SdkError(response.status_code, _error_message(response))
        return response.json()

    # 列出 durable threads
    async def list_threads(self) -> list[ThreadRecord]:
        return _validate_public_list(
            ThreadRecord, await self._request("GET", "/v1/threads")
        )

    # 创建 durable thread
    async def create_thread(self, title: str = "", mode: str = "chat") -> ThreadRecord:
        payload = await self._request(
            "POST",
            "/v1/threads",
            json={"title": title, "mode": mode},
        )
        return _validate_public_model(ThreadRecord, payload)

    # 读取单个 durable thread
    async def get_thread(self, thread_id: str) -> ThreadRecord:
        return _validate_public_model(
            ThreadRecord, await self._request("GET", f"/v1/threads/{thread_id}")
        )

    # 更新 thread 标题或将其归档
    async def update_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ThreadRecord:
        body = {
            key: value
            for key, value in {"title": title, "archived": archived}.items()
            if value is not None
        }
        return _validate_public_model(
            ThreadRecord,
            await self._request("PATCH", f"/v1/threads/{thread_id}", json=body),
        )

    # 列出 thread 的 durable turns
    async def list_turns(self, thread_id: str) -> list[TurnRecord]:
        payload = await self._request("GET", f"/v1/threads/{thread_id}/turns")
        return _validate_public_list(TurnRecord, payload)

    # 读取单个 durable turn
    async def get_turn(self, turn_id: str) -> TurnRecord:
        return _validate_public_model(
            TurnRecord, await self._request("GET", f"/v1/turns/{turn_id}")
        )

    # 在指定 thread 启动 turn
    async def start_turn(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode = RuntimeMode.ACT,
    ) -> TurnRecord:
        payload = await self._request(
            "POST",
            f"/v1/threads/{thread_id}/turns",
            json={"content": content, "mode": mode.value},
        )
        return _validate_public_model(TurnRecord, payload)

    # 中断活动 turn
    async def interrupt_turn(self, turn_id: str) -> TurnRecord:
        payload = await self._request("POST", f"/v1/turns/{turn_id}/interrupt")
        return _validate_public_model(TurnRecord, payload)

    # 向活动 turn 注入 steering 内容
    async def steer_turn(self, turn_id: str, content: str) -> TurnRecord:
        payload = await self._request(
            "POST",
            f"/v1/turns/{turn_id}/steer",
            json={"content": content},
        )
        return _validate_public_model(TurnRecord, payload)

    # 读取 turn 的持久 item
    async def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        payload = await self._request("GET", f"/v1/turns/{turn_id}/items")
        return _validate_public_list(TurnItemRecord, payload)

    # 读取可离线审计的 turn receipt
    async def get_receipt(self, turn_id: str) -> TurnReceipt:
        payload = await self._request("GET", f"/v1/turns/{turn_id}/receipt")
        return _validate_public_model(TurnReceipt, payload)

    # 响应工具审批，可附带逐 hunk 选择与 PatchPlan 标识
    async def respond_permission(
        self,
        tool_use_id: str,
        decision: str,
        *,
        session_id: str | None = None,
        selected_hunks: list[str] | None = None,
        patch_plan_id: str | None = None,
    ) -> bool:
        payload = await self._request(
            "POST",
            f"/v1/permissions/{tool_use_id}",
            json={
                "decision": decision,
                "session_id": session_id,
                "selected_hunks": selected_hunks,
                "patch_plan_id": patch_plan_id,
            },
        )
        return bool(payload.get("accepted"))

    # 读取结构化工作区 diff 供 IDE 展示
    async def workspace_diff(
        self,
        *,
        scope: str = "all",
        path: str = ".",
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            "/v1/workspace/diff",
            params={"scope": scope, "path": path},
        )
        return dict(payload)

    # 读取服务端版本与可协商能力
    async def capabilities(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/v1/capabilities"))

    # 读取 durable turn 的聚合用量
    async def usage(self) -> dict[str, Any]:
        return dict(await self._request("GET", "/v1/usage"))

    # 以 Last-Event-ID 游标消费 SSE；断线时从最后提交序号继续
    async def events(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
        reconnect: bool = True,
        reconnect_delay: float = 0.2,
    ) -> AsyncIterator[RuntimeEventRecord]:
        cursor = after_seq
        while True:
            try:
                async with self._client.stream(
                    "GET",
                    f"/v1/threads/{thread_id}/events",
                    headers={"Last-Event-ID": str(cursor)},
                ) as response:
                    if not response.is_success:
                        await response.aread()
                        raise SdkError(response.status_code, _error_message(response))
                    data_lines: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data_lines.append(line.removeprefix("data:").lstrip())
                        elif not line and data_lines:
                            event = _validate_public_model(
                                RuntimeEventRecord,
                                json.loads("\n".join(data_lines)),
                            )
                            data_lines.clear()
                            if event.seq <= cursor:
                                continue
                            cursor = event.seq
                            yield event
            except (httpx.TransportError, httpx.TimeoutException):
                if not reconnect:
                    raise
            if not reconnect:
                return
            await asyncio.sleep(reconnect_delay)


class CodeRookClient:
    # 初始化同步 HTTP/SSE 客户端，与异步版共享同一服务端模型
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    # 关闭底层连接池
    def close(self) -> None:
        self._client.close()

    # 进入同步上下文
    def __enter__(self) -> CodeRookClient:
        return self

    # 离开同步上下文并关闭连接
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    # 执行同步 JSON 请求并统一转换非 2xx 错误
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._client.request(method, path, **kwargs)
        if not response.is_success:
            raise SdkError(response.status_code, _error_message(response))
        return response.json()

    # 列出 durable threads
    def list_threads(self) -> list[ThreadRecord]:
        return _validate_public_list(ThreadRecord, self._request("GET", "/v1/threads"))

    # 创建 durable thread
    def create_thread(self, title: str = "", mode: str = "chat") -> ThreadRecord:
        payload = self._request(
            "POST",
            "/v1/threads",
            json={"title": title, "mode": mode},
        )
        return _validate_public_model(ThreadRecord, payload)

    # 读取单个 durable thread
    def get_thread(self, thread_id: str) -> ThreadRecord:
        return _validate_public_model(
            ThreadRecord, self._request("GET", f"/v1/threads/{thread_id}")
        )

    # 更新 thread 标题或将其归档
    def update_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ThreadRecord:
        body = {
            key: value
            for key, value in {"title": title, "archived": archived}.items()
            if value is not None
        }
        return _validate_public_model(
            ThreadRecord,
            self._request("PATCH", f"/v1/threads/{thread_id}", json=body),
        )

    # 列出 thread 的 durable turns
    def list_turns(self, thread_id: str) -> list[TurnRecord]:
        return _validate_public_list(
            TurnRecord, self._request("GET", f"/v1/threads/{thread_id}/turns")
        )

    # 读取单个 durable turn
    def get_turn(self, turn_id: str) -> TurnRecord:
        return _validate_public_model(
            TurnRecord, self._request("GET", f"/v1/turns/{turn_id}")
        )

    # 在指定 thread 启动 turn
    def start_turn(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode = RuntimeMode.ACT,
    ) -> TurnRecord:
        payload = self._request(
            "POST",
            f"/v1/threads/{thread_id}/turns",
            json={"content": content, "mode": mode.value},
        )
        return _validate_public_model(TurnRecord, payload)

    # 中断活动 turn
    def interrupt_turn(self, turn_id: str) -> TurnRecord:
        return _validate_public_model(
            TurnRecord,
            self._request("POST", f"/v1/turns/{turn_id}/interrupt"),
        )

    # 向活动 turn 注入 steering 内容
    def steer_turn(self, turn_id: str, content: str) -> TurnRecord:
        return _validate_public_model(
            TurnRecord,
            self._request(
                "POST",
                f"/v1/turns/{turn_id}/steer",
                json={"content": content},
            ),
        )

    # 读取 turn 的持久 item
    def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        return _validate_public_list(
            TurnItemRecord, self._request("GET", f"/v1/turns/{turn_id}/items")
        )

    # 读取可离线审计的 turn receipt
    def get_receipt(self, turn_id: str) -> TurnReceipt:
        return _validate_public_model(
            TurnReceipt, self._request("GET", f"/v1/turns/{turn_id}/receipt")
        )

    # 同步响应工具审批，可附带逐 hunk 选择
    def respond_permission(
        self,
        tool_use_id: str,
        decision: str,
        *,
        session_id: str | None = None,
        selected_hunks: list[str] | None = None,
        patch_plan_id: str | None = None,
    ) -> bool:
        payload = self._request(
            "POST",
            f"/v1/permissions/{tool_use_id}",
            json={
                "decision": decision,
                "session_id": session_id,
                "selected_hunks": selected_hunks,
                "patch_plan_id": patch_plan_id,
            },
        )
        return bool(payload.get("accepted"))

    # 同步读取结构化工作区 diff
    def workspace_diff(
        self,
        *,
        scope: str = "all",
        path: str = ".",
    ) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/v1/workspace/diff",
            params={"scope": scope, "path": path},
        )
        return dict(payload)

    # 同步读取服务端版本与可协商能力
    def capabilities(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/capabilities"))

    # 同步读取 durable turn 的聚合用量
    def usage(self) -> dict[str, Any]:
        return dict(self._request("GET", "/v1/usage"))

    # 同步消费 SSE，并通过 Last-Event-ID 在断线后续接
    def events(
        self,
        thread_id: str,
        *,
        after_seq: int = 0,
    ) -> Iterator[RuntimeEventRecord]:
        cursor = after_seq
        with self._client.stream(
            "GET",
            f"/v1/threads/{thread_id}/events",
            headers={"Last-Event-ID": str(cursor)},
        ) as response:
            if not response.is_success:
                response.read()
                raise SdkError(response.status_code, _error_message(response))
            data_lines: list[str] = []
            for line in response.iter_lines():
                if line.startswith("data:"):
                    data_lines.append(line.removeprefix("data:").lstrip())
                elif not line and data_lines:
                    event = _validate_public_model(
                        RuntimeEventRecord,
                        json.loads("\n".join(data_lines)),
                    )
                    data_lines.clear()
                    if event.seq <= cursor:
                        continue
                    cursor = event.seq
                    yield event

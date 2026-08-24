from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict

from code_rook.core.capabilities import (
    CapabilityContribution,
    CapabilityKernel,
    CapabilityKind,
    CapabilityScope,
    CapabilityStability,
    ContributionHandle,
)
from code_rook.core.processes import (
    explicit_extension_environment,
    terminate_process_tree,
)


class WorkerBackendCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    one_shot: bool = True
    continuation: bool = False
    structured_output: bool = False
    persona: bool = False
    tool_restriction: bool = False
    read_only_guarantee: bool = False
    persistent_resume: bool = False
    live_events: bool = True


@dataclass(frozen=True)
class WorkerLaunchSpec:
    worker_id: str
    prompt: str
    cwd: Path
    read_only: bool
    env: dict[str, str]


@dataclass(frozen=True)
class WorkerBackendResult:
    status: str
    output: str
    diagnostic: str = ""


class WorkerHandle(Protocol):
    # 返回指定游标后的只读事件快照
    def events(self, after_cursor: int = 0) -> tuple[dict[str, Any], ...]: ...

    # 向支持 continuation 的外部 Agent 发送后续消息
    async def followup(self, message: str) -> None: ...

    # 请求取消外部 Agent 的当前工作
    async def cancel(self) -> None: ...

    # 等待外部 Agent 的一次终态结果
    async def result(self) -> WorkerBackendResult: ...

    # 释放进程、读取任务和其他句柄
    async def dispose(self) -> None: ...


class WorkerBackend(Protocol):
    name: str
    capabilities: WorkerBackendCapabilities

    # 建立一个已经隔离到受管 Worktree 的 Worker
    async def start(self, spec: WorkerLaunchSpec) -> WorkerHandle: ...

    # 尝试恢复持久 Worker；不支持时明确返回 None
    async def restore(self, worker_id: str) -> WorkerHandle | None: ...


class WorkerBackendRegistry:
    # 初始化由 CapabilityKernel 支撑的 Backend 注册表和活跃 Worker 句柄表
    def __init__(
        self,
        capability_kernel: CapabilityKernel | None = None,
        *,
        scope: CapabilityScope = CapabilityScope(),
    ) -> None:
        self._capability_kernel = capability_kernel
        self._scope = scope
        self._backends: dict[str, WorkerBackend] = {}
        self._backend_handles: dict[str, ContributionHandle] = {}
        self._handles: dict[str, WorkerHandle] = {}

    # 注册唯一 Backend 并返回幂等撤销函数
    def register(self, backend: WorkerBackend) -> Callable[[], None]:
        if not backend.name or backend.name in self._backends:
            raise ValueError(f"duplicate or empty worker backend: {backend.name}")
        if self._capability_kernel is not None:
            handle = self._capability_kernel.register(
                CapabilityContribution(
                    id=backend.name,
                    kind=CapabilityKind.WORKER_BACKEND.value,
                    provider=backend,
                    stability=CapabilityStability.LABS,
                    scope=self._scope,
                )
            )
            self._backend_handles[backend.name] = handle
        self._backends[backend.name] = backend

        # 撤销当前 Backend，已发布句柄仍由运行控制面负责释放
        def dispose() -> None:
            self._backends.pop(backend.name, None)
            handle = self._backend_handles.pop(backend.name, None)
            if handle is not None:
                handle.dispose()

        return dispose

    # 返回命名 Backend，不存在时明确失败
    def require(self, name: str) -> WorkerBackend:
        if self._capability_kernel is not None:
            resolved = self._capability_kernel.resolve(
                CapabilityKind.WORKER_BACKEND,
                name,
                self._scope,
            )
            if resolved is not None:
                return cast(WorkerBackend, resolved)
        try:
            return self._backends[name]
        except KeyError:
            raise ValueError(f"worker backend is unavailable: {name}") from None

    # 校验请求能力后启动 Worker 并发布句柄所有权
    async def start(
        self,
        name: str,
        spec: WorkerLaunchSpec,
        *,
        require_continuation: bool = False,
        require_read_only: bool = False,
    ) -> WorkerHandle:
        backend = self.require(name)
        if require_continuation and not backend.capabilities.continuation:
            raise ValueError(f"worker backend does not support continuation: {name}")
        if require_read_only and not backend.capabilities.read_only_guarantee:
            raise ValueError(f"worker backend cannot guarantee read-only execution: {name}")
        handle = await backend.start(spec)
        self._handles[spec.worker_id] = handle
        return handle

    # 返回已发布的活跃 Worker 句柄
    def handle(self, worker_id: str) -> WorkerHandle | None:
        return self._handles.get(worker_id)

    # 释放并移除一个 Worker 句柄
    async def dispose_handle(self, worker_id: str) -> None:
        handle = self._handles.pop(worker_id, None)
        if handle is not None:
            await handle.dispose()

    # 释放全部活跃 Worker 并撤销 Backend capability contribution
    async def close(self) -> None:
        for worker_id in tuple(self._handles):
            await self.dispose_handle(worker_id)
        for backend_name in tuple(self._backend_handles):
            handle = self._backend_handles.pop(backend_name)
            handle.dispose()
        self._backends.clear()


class AcpWorkerBackend:
    name = "acp"
    capabilities = WorkerBackendCapabilities(
        one_shot=True,
        continuation=False,
        structured_output=False,
        persona=False,
        tool_restriction=False,
        read_only_guarantee=False,
        persistent_resume=False,
        live_events=True,
    )

    # 固定用户进程显式配置的 ACP 命令和环境白名单
    def __init__(
        self,
        command: tuple[str, ...] | str,
        *,
        env: dict[str, str] | None = None,
        startup_timeout_s: float = 20.0,
    ) -> None:
        parsed = _parse_acp_command(command) if isinstance(command, str) else command
        if not parsed:
            raise ValueError("ACP worker command must not be empty")
        self._command = parsed
        self._env = dict(env or {})
        self._startup_timeout_s = max(1.0, startup_timeout_s)

    # 启动凭据清洗后的 ACP 子进程并完成 initialize/session/new 握手
    async def start(self, spec: WorkerLaunchSpec) -> WorkerHandle:
        environment = explicit_extension_environment({**self._env, **spec.env})
        process = await asyncio.create_subprocess_exec(
            *self._command,
            cwd=spec.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        handle = _AcpWorkerHandle(process, spec.worker_id)
        try:
            await asyncio.wait_for(
                handle.initialize(spec.cwd, spec.prompt),
                timeout=self._startup_timeout_s,
            )
        except BaseException:
            await handle.dispose()
            raise
        return handle

    # ACP 首发不承诺跨 daemon 恢复，明确返回不支持
    async def restore(self, worker_id: str) -> WorkerHandle | None:
        return None


class _AcpWorkerHandle:
    # 初始化 ACP JSON-RPC 读取泵、请求表和有界事件历史
    def __init__(self, process: asyncio.subprocess.Process, worker_id: str) -> None:
        self._process = process
        self._worker_id = worker_id
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._session_id = ""
        self._result: asyncio.Future[WorkerBackendResult] = (
            asyncio.get_running_loop().create_future()
        )
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr = asyncio.create_task(self._read_stderr())

    # 完成 ACP 初始化、创建 session 并提交首条 prompt
    async def initialize(self, cwd: Path, prompt: str) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
            },
        )
        created = await self._request(
            "session/new",
            {"cwd": str(cwd), "mcpServers": []},
        )
        if not isinstance(created, dict) or not isinstance(created.get("sessionId"), str):
            raise RuntimeError("ACP session/new returned no sessionId")
        self._session_id = created["sessionId"]
        asyncio.create_task(self._submit_prompt(prompt))

    # 提交 prompt 并把 ACP 响应归一为 Worker 终态
    async def _submit_prompt(self, prompt: str) -> None:
        try:
            response = await self._request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": prompt}],
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._result.done():
                self._result.set_result(
                    WorkerBackendResult("failed", "", type(exc).__name__)
                )
            return
        output = _extract_acp_output(response)
        if not self._result.done():
            self._result.set_result(WorkerBackendResult("completed", output))

    # 发送一个 JSON-RPC 请求并等待匹配响应
    async def _request(self, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    # 向 ACP 进程写入一条紧凑 JSON-RPC 行
    async def _write(self, payload: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise RuntimeError("ACP stdin is unavailable")
        self._process.stdin.write(
            (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        )
        await self._process.stdin.drain()

    # 读取 ACP stdout，分派响应并保留最近 1000 条通知
    async def _read_stdout(self) -> None:
        if self._process.stdout is None:
            return
        while line := await self._process.stdout.readline():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            raw_id = payload.get("id")
            if isinstance(raw_id, int) and raw_id in self._pending:
                future = self._pending.pop(raw_id)
                if "error" in payload:
                    future.set_exception(RuntimeError(str(payload["error"])))
                else:
                    future.set_result(payload.get("result"))
                continue
            self._events.append(payload)
            if len(self._events) > 1000:
                del self._events[:-1000]
        if not self._result.done():
            return_code = await self._process.wait()
            self._result.set_result(
                WorkerBackendResult(
                    "failed" if return_code else "completed",
                    "",
                    f"ACP process exited with code {return_code}",
                )
            )

    # 有界读取 stderr 并作为诊断事件保存，不向结果暴露环境或请求体
    async def _read_stderr(self) -> None:
        if self._process.stderr is None:
            return
        while line := await self._process.stderr.readline():
            text = line.decode("utf-8", errors="replace").strip()
            self._events.append({"type": "diagnostic", "message": text[:4096]})
            if len(self._events) > 1000:
                del self._events[:-1000]

    # 返回指定游标后的事件副本
    def events(self, after_cursor: int = 0) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._events[max(0, after_cursor) :])

    # 通过同一 ACP session 提交后续 prompt
    async def followup(self, message: str) -> None:
        if not self._session_id:
            raise RuntimeError("ACP session is not ready")
        await self._submit_prompt(message)

    # 终止 ACP 进程树并把未完成结果标记为 cancelled
    async def cancel(self) -> None:
        await terminate_process_tree(self._process)
        if not self._result.done():
            self._result.set_result(WorkerBackendResult("cancelled", ""))

    # 等待 ACP prompt 的终态结果
    async def result(self) -> WorkerBackendResult:
        return await self._result

    # 幂等停止读取泵、拒绝未决请求并释放进程树
    async def dispose(self) -> None:
        if self._process.returncode is None:
            await terminate_process_tree(self._process)
        for task in (self._reader, self._stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(self._reader, self._stderr, return_exceptions=True)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RuntimeError("ACP worker disposed"))
        self._pending.clear()


# 从不同 ACP Agent 的 prompt 结果形状中提取安全文本摘要
def _extract_acp_output(response: Any) -> str:
    if isinstance(response, str):
        return response
    if not isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False)
    for key in ("output", "text", "message"):
        value = response.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(response, ensure_ascii=False, separators=(",", ":"))


# 解析用户显式 ACP argv，Windows 优先支持 JSON 数组以无损表达带空格和反斜杠的路径
def _parse_acp_command(command: str) -> tuple[str, ...]:
    stripped = command.strip()
    if stripped.startswith("["):
        raw = json.loads(stripped)
        if not isinstance(raw, list) or not all(
            isinstance(item, str) and item for item in raw
        ):
            raise ValueError("ACP JSON command must be a non-empty string array")
        return tuple(raw)
    parsed = shlex.split(stripped, posix=os.name != "nt")
    if os.name == "nt":
        parsed = [item.strip('"') for item in parsed]
    return tuple(parsed)

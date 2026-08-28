from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.artifacts import ArtifactError, ArtifactSpool, ArtifactStore
from code_rook.core.processes import (
    ProcessSupervisor,
    _decode_shell_output,
    sanitized_shell_environment,
    terminate_process_tree,
)
from code_rook.core.sandbox.planner import SandboxPlan, persistent_sandbox_argv
from code_rook.core.tools.execution_metadata import report_tool_progress

_IDLE_RECYCLE_S = 1800.0
_MAX_SESSION_OUTPUT_BYTES = 64 * 1024
_EXIT_TAIL_RE = re.compile(rb"_(\d+)\r?\n")
_READ_CHUNK_BYTES = 64 * 1024


# 返回平台默认常驻 shell 命令行
def default_shell_argv() -> list[str]:
    if os.name == "nt":
        return ["cmd", "/Q"]
    return ["/bin/sh"]


@dataclass(frozen=True)
class ShellRunOutcome:
    # 单条命令在常驻 shell 中的执行结果
    text: str
    exit_code: int | None
    truncated: bool = False
    timed_out: bool = False
    died: bool = False
    job_id: str = ""
    process_usage: dict[str, object] | None = None
    output_bytes: int = 0
    output_artifact: str = ""
    output_artifact_size: int = 0
    output_artifact_error: str = ""


class PersistentShellSession:
    # 单个常驻 shell 进程：命令经 stdin 注入并以 sentinel 行探测完成与退出码

    def __init__(
        self,
        argv: list[str] | None = None,
        cwd: Path | None = None,
        *,
        process_supervisor: ProcessSupervisor | None = None,
        label: str = "persistent-shell",
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._argv = argv or default_shell_argv()
        self._cwd = cwd
        self._is_cmd = any(
            os.path.basename(item).lower() in {"cmd", "cmd.exe"}
            for item in self._argv
        )
        self._proc: asyncio.subprocess.Process | None = None
        # 有界队列向 OS pipe 施加背压，后台持续输出不能无限占用 daemon 内存
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self._reader: asyncio.Task[None] | None = None
        # alive 表示"未终止"；进程在首条命令时惰性启动
        self._alive = True
        self._marker = f"__CODEROOK_{secrets.token_hex(8)}"
        self.last_used = time.monotonic()
        self._process_supervisor = process_supervisor
        self._label = label
        self._command_sequence = 0
        self._command_lock = asyncio.Lock()
        self._carryover = bytearray()
        self._artifact_store = artifact_store
        self._active_spool: ArtifactSpool | None = None

    # 返回会话进程是否仍在运行
    @property
    def alive(self) -> bool:
        return self._alive

    # 返回决定进程权限边界的 argv 与工作目录身份，供池拒绝跨策略复用
    @property
    def execution_identity(self) -> tuple[tuple[str, ...], str]:
        cwd = str(self._cwd.resolve()) if self._cwd is not None else ""
        return tuple(self._argv), cwd

    # 惰式启动常驻进程并消费 shell 启动横幅
    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        platform_options: dict[str, object] = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        if self._process_supervisor is not None:
            self._proc = await self._process_supervisor.start_exec(
                *self._argv,
                label=self._label,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sanitized_shell_environment(),
            )
        else:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=sanitized_shell_environment(),
                **platform_options,  # type: ignore[arg-type]
            )
        self._alive = True
        self._reader = asyncio.create_task(self._read_loop())
        await self._send_and_collect(
            "rem persistent shell ready",
            timeout_s=10.0,
            job_id=f"{self._label}:startup",
        )

    # 持续把 stdout 分块推入队列，支持任意长度的无换行输出
    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            chunk = await proc.stdout.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            await self._queue.put(chunk)
        self._alive = False
        await self._queue.put(b"")
        if self._process_supervisor is not None:
            self._process_supervisor.forget(proc)

    # 发送一条命令并收集到 sentinel 行，返回原始输出与退出码
    async def _send_and_collect(
        self,
        command: str,
        *,
        timeout_s: float,
        job_id: str,
    ) -> ShellRunOutcome:
        command_started = time.monotonic()
        self.last_used = time.monotonic()
        await self._ensure_started()
        proc = self._proc
        if proc is None or proc.stdin is None:
            return ShellRunOutcome(text="", exit_code=None, died=True, job_id=job_id)
        before_usage = (
            self._process_supervisor.usage(proc)
            if self._process_supervisor is not None
            else None
        )
        if self._is_cmd:
            wrapped = f"{command}\n@echo {self._marker}_%ERRORLEVEL%\n"
        else:
            wrapped = (
                f"{command}\n"
                "__coderook_status=$?\n"
                "for __coderook_pid in $(jobs -p 2>/dev/null); do "
                'kill "$__coderook_pid" 2>/dev/null; done\n'
                "wait 2>/dev/null\n"
                f'echo "{self._marker}_$__coderook_status"\n'
            )
        proc.stdin.write(wrapped.encode("utf-8", errors="replace"))
        await proc.stdin.drain()

        output = bytearray()
        output_spool = ArtifactSpool(self._artifact_store)
        self._active_spool = output_spool
        pending = bytearray(self._carryover)
        self._carryover.clear()
        truncated = False
        exit_code: int | None = None
        timed_out = False
        died = False
        marker_bytes = self._marker.encode()
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                break
            if not chunk:
                died = True
                break
            pending.extend(chunk)
            marker_start = pending.find(marker_bytes)
            if marker_start >= 0:
                match = _EXIT_TAIL_RE.search(pending, marker_start + len(marker_bytes))
                if match is not None:
                    complete = bytes(pending[:marker_start])
                    output_spool.write(complete)
                    self._append_bounded_output(
                        output,
                        complete,
                    )
                    try:
                        exit_code = int(match.group(1))
                    except ValueError:
                        exit_code = None
                    self._carryover.extend(pending[match.end():])
                    break
            safe_length = max(0, len(pending) - len(marker_bytes) - 32)
            if safe_length:
                safe = bytes(pending[:safe_length])
                output_spool.write(safe)
                self._append_bounded_output(output, safe)
                del pending[:safe_length]
            progress_tail = (bytes(output[-4096:]) + bytes(pending[-4096:]))[-4096:]
            await report_tool_progress(
                _decode_shell_output(progress_tail),
                output_spool.size + len(pending),
            )
        if exit_code is None and pending:
            output_spool.write(bytes(pending))
            self._append_bounded_output(output, bytes(pending))
        truncated = output_spool.size > len(output)
        after_usage = (
            self._process_supervisor.usage(proc)
            if self._process_supervisor is not None
            else None
        )
        process_usage: dict[str, object] | None = None
        if after_usage is not None:
            usage_payload = after_usage.to_dict()
            usage_payload["wall_time_ms"] = max(
                0,
                int((time.monotonic() - command_started) * 1000),
            )
            usage_payload["job_id"] = job_id
            if before_usage is not None:
                usage_payload["user_cpu_ms"] = max(
                    0,
                    after_usage.user_cpu_ms - before_usage.user_cpu_ms,
                )
                usage_payload["system_cpu_ms"] = max(
                    0,
                    after_usage.system_cpu_ms - before_usage.system_cpu_ms,
                )
            process_usage = usage_payload
        if timed_out or died:
            await self._terminate()
        artifact_handle = ""
        artifact_size = 0
        artifact_error = ""
        try:
            artifact = await output_spool.finish(persist=truncated)
        except (ArtifactError, OSError) as exc:
            artifact_error = getattr(exc, "code", "artifact_error")
        else:
            if artifact is not None:
                artifact_handle = artifact.handle
                artifact_size = artifact.size
        finally:
            self._active_spool = None
        text = _decode_shell_output(bytes(output))
        return ShellRunOutcome(
            text=text,
            exit_code=exit_code,
            truncated=truncated,
            timed_out=timed_out,
            died=died,
            job_id=job_id,
            process_usage=process_usage,
            output_bytes=output_spool.size,
            output_artifact=artifact_handle,
            output_artifact_size=artifact_size,
            output_artifact_error=artifact_error,
        )

    # 向固定容量缓冲区追加输出并丢弃超出容量的尾部
    @staticmethod
    def _append_bounded_output(output: bytearray, data: bytes) -> None:
        room = max(0, _MAX_SESSION_OUTPUT_BYTES - len(output))
        if room:
            output.extend(data[:room])

    # 在常驻会话中执行一条命令，保留 cwd/env 状态
    async def run(self, command: str, *, timeout_s: float) -> ShellRunOutcome:
        async with self._command_lock:
            self._command_sequence += 1
            try:
                return await self._send_and_collect(
                    command,
                    timeout_s=timeout_s,
                    job_id=f"{self._label}:job-{self._command_sequence}",
                )
            except asyncio.CancelledError:
                if self._active_spool is not None:
                    self._active_spool.discard()
                    self._active_spool = None
                await asyncio.shield(self._terminate())
                raise
            except Exception:
                if self._active_spool is not None:
                    self._active_spool.discard()
                    self._active_spool = None
                raise

    # 终止常驻进程并标记会话失效
    async def _terminate(self) -> None:
        self._alive = False
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except (OSError, RuntimeError):
                    pass
            try:
                if self._process_supervisor is not None:
                    await self._process_supervisor.terminate(proc)
                else:
                    await terminate_process_tree(proc)
            except (OSError, RuntimeError, ProcessLookupError):
                pass
            self._proc = None
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not asyncio.current_task():
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


class PersistentShellPool:
    # 按 key（通常为 session_id）复用常驻 shell，空闲超时回收

    def __init__(
        self,
        *,
        idle_recycle_s: float = _IDLE_RECYCLE_S,
        process_supervisor: ProcessSupervisor | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._sessions: dict[str, PersistentShellSession] = {}
        self._idle_recycle_s = idle_recycle_s
        self._process_supervisor = process_supervisor
        self._artifact_store = artifact_store

    # 返回 key 对应的活跃会话，失效或缺失时新建；顺带回收空闲会话
    def get_or_create(
        self,
        key: str,
        cwd: Path | None = None,
        sandbox_plan: SandboxPlan | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> PersistentShellSession:
        self._sweep()
        sandbox_argv = (
            persistent_sandbox_argv(sandbox_plan)
            if sandbox_plan is not None
            else None
        )
        desired_argv = sandbox_argv or default_shell_argv()
        desired_cwd = str(cwd.resolve()) if cwd is not None else ""
        desired_identity = tuple(desired_argv), desired_cwd
        session = self._sessions.get(key)
        if session is not None and (
            not session.alive or session.execution_identity != desired_identity
        ):
            self._sessions.pop(key, None)
            if session.alive:
                self._schedule_terminate(session)
            session = None
        if session is None:
            session = PersistentShellSession(
                argv=desired_argv,
                cwd=cwd,
                process_supervisor=self._process_supervisor,
                label=f"persistent-shell:{key}",
                artifact_store=artifact_store or self._artifact_store,
            )
            self._sessions[key] = session
        return session

    # 在当前事件循环中异步终止被回收的 shell，避免同步池 API 泄漏旧策略进程
    def _schedule_terminate(self, session: PersistentShellSession) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            session._alive = False
            return
        loop.create_task(session._terminate())

    # 关闭空闲超过阈值的活跃会话，避免长期驻留进程
    def _sweep(self) -> None:
        now = time.monotonic()
        dead_keys = [
            key
            for key, session in self._sessions.items()
            if not session.alive or now - session.last_used > self._idle_recycle_s
        ]
        for key in dead_keys:
            session = self._sessions.pop(key, None)
            if session is not None and session.alive:
                self._schedule_terminate(session)

    # 关闭全部会话（daemon 停机时调用）
    async def aclose_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session._terminate()

    # 返回池内当前会话数，供测试与观测
    def size(self) -> int:
        return len(self._sessions)

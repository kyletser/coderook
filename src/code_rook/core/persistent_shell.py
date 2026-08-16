from __future__ import annotations

import asyncio
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.processes import _decode_shell_output, terminate_process_tree

_IDLE_RECYCLE_S = 1800.0
_MAX_SESSION_OUTPUT_BYTES = 64 * 1024
_EXIT_TAIL_RE = re.compile(rb"_(\d+)\s*$")


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


class PersistentShellSession:
    # 单个常驻 shell 进程：命令经 stdin 注入并以 sentinel 行探测完成与退出码

    def __init__(self, argv: list[str] | None = None, cwd: Path | None = None) -> None:
        self._argv = argv or default_shell_argv()
        self._cwd = cwd
        self._is_cmd = os.path.basename(self._argv[0]).lower() in {"cmd", "cmd.exe"}
        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._reader: asyncio.Task[None] | None = None
        # alive 表示"未终止"；进程在首条命令时惰性启动
        self._alive = True
        self._marker = f"__CODEROOK_{secrets.token_hex(8)}"
        self.last_used = time.monotonic()

    # 返回会话进程是否仍在运行
    @property
    def alive(self) -> bool:
        return self._alive

    # 惰式启动常驻进程并消费 shell 启动横幅
    async def _ensure_started(self) -> None:
        if self._proc is not None:
            return
        platform_options: dict[str, object] = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **platform_options,  # type: ignore[arg-type]
        )
        self._alive = True
        self._reader = asyncio.create_task(self._read_loop())
        await self._send_and_collect("rem persistent shell ready", timeout_s=10.0)

    # 持续把 stdout 行推入队列，进程退出时投递 EOF 空行
    async def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            await self._queue.put(line)
        await self._queue.put(b"")

    # 发送一条命令并收集到 sentinel 行，返回原始输出与退出码
    async def _send_and_collect(self, command: str, *, timeout_s: float) -> ShellRunOutcome:
        self.last_used = time.monotonic()
        await self._ensure_started()
        proc = self._proc
        if proc is None or proc.stdin is None:
            return ShellRunOutcome(text="", exit_code=None, died=True)
        if self._is_cmd:
            wrapped = f"{command}\n@echo {self._marker}_%ERRORLEVEL%\n"
        else:
            wrapped = f'{command}\necho "{self._marker}_$?"\n'
        proc.stdin.write(wrapped.encode("utf-8", errors="replace"))
        await proc.stdin.drain()

        output = bytearray()
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
                line = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                timed_out = True
                break
            if not line:
                died = True
                break
            if marker_bytes in line:
                match = _EXIT_TAIL_RE.search(line)
                if match is not None:
                    try:
                        exit_code = int(match.group(1))
                    except ValueError:
                        exit_code = None
                break
            if len(output) < _MAX_SESSION_OUTPUT_BYTES:
                room = _MAX_SESSION_OUTPUT_BYTES - len(output)
                output.extend(line[:room])
                if len(line) > room:
                    truncated = True
            else:
                truncated = True
        if timed_out or died:
            await self._terminate()
        text = _decode_shell_output(bytes(output))
        return ShellRunOutcome(
            text=text,
            exit_code=exit_code,
            truncated=truncated,
            timed_out=timed_out,
            died=died,
        )

    # 在常驻会话中执行一条命令，保留 cwd/env 状态
    async def run(self, command: str, *, timeout_s: float) -> ShellRunOutcome:
        return await self._send_and_collect(command, timeout_s=timeout_s)

    # 终止常驻进程并标记会话失效
    async def _terminate(self) -> None:
        self._alive = False
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (OSError, RuntimeError):
                    pass
            try:
                await terminate_process_tree(proc)
            except (OSError, RuntimeError, ProcessLookupError):
                pass
            self._proc = None
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            self._reader = None


class PersistentShellPool:
    # 按 key（通常为 session_id）复用常驻 shell，空闲超时回收

    def __init__(self, *, idle_recycle_s: float = _IDLE_RECYCLE_S) -> None:
        self._sessions: dict[str, PersistentShellSession] = {}
        self._idle_recycle_s = idle_recycle_s

    # 返回 key 对应的活跃会话，失效或缺失时新建；顺带回收空闲会话
    def get_or_create(self, key: str, cwd: Path | None = None) -> PersistentShellSession:
        self._sweep()
        session = self._sessions.get(key)
        if session is not None and not session.alive:
            self._sessions.pop(key, None)
            session = None
        if session is None:
            session = PersistentShellSession(cwd=cwd)
            self._sessions[key] = session
        return session

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
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    continue
                loop.create_task(session._terminate())

    # 关闭全部会话（daemon 停机时调用）
    async def aclose_all(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session._terminate()

    # 返回池内当前会话数，供测试与观测
    def size(self) -> int:
        return len(self._sessions)

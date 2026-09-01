from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from code_rook.core.artifacts import ArtifactError, ArtifactSpool, ArtifactStore
from code_rook.core.bus.events import BackgroundJobFinishedEvent, BackgroundJobStartedEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.processes import (
    ProcessSupervisor,
    bounded_shell_output,
    wait_for_process_leader,
)
from code_rook.core.sandbox.planner import (
    SandboxPlan,
    SandboxSpawnRequest,
    spawn_sandboxed_shell,
)

_DEFAULT_OUTPUT_LIMIT = 64 * 1024
_READ_CHUNK_SIZE = 8 * 1024
_DEFAULT_HISTORY_LIMIT = 100


# 返回当前 UTC ISO 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class BackgroundJob:
    id: str
    command: str
    session_id: str
    run_id: str
    status: str = "running"
    output: str = ""
    is_error: bool = False
    created_at: str = ""
    finished_at: str = ""
    sandbox_backend: str = "degraded"
    cwd: str = ""
    process_usage: dict[str, object] | None = None
    output_bytes: int = 0
    output_truncated: bool = False
    output_artifact: str = ""
    output_artifact_size: int = 0
    output_artifact_error: str = ""


class _OutputRing:
    # 初始化仅保留最新字节的固定容量输出环
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._buffer = bytearray()
        self.total_bytes = 0
        self.truncated = False

    # 追加一个输出块并从头部淘汰超出容量的旧字节
    def append(self, chunk: bytes) -> None:
        self.total_bytes += len(chunk)
        if len(chunk) >= self._max_bytes:
            self._buffer = bytearray(chunk[-self._max_bytes :])
            self.truncated = self.total_bytes > self._max_bytes
            return
        overflow = len(self._buffer) + len(chunk) - self._max_bytes
        if overflow > 0:
            del self._buffer[:overflow]
            self.truncated = True
        self._buffer.extend(chunk)

    # 将当前尾部缓冲解码为可展示文本并明确标注早期输出已被截断
    def render(self) -> str:
        output, _truncated = bounded_shell_output(bytes(self._buffer))
        if self.truncated:
            return f"[earlier output truncated]\n{output}"
        return output


class BackgroundJobRegistry:
    # 初始化 daemon 级后台任务表和事件总线
    def __init__(
        self,
        bus: EventBus,
        process_supervisor: ProcessSupervisor | None = None,
        *,
        max_output_bytes: int = _DEFAULT_OUTPUT_LIMIT,
        artifact_store: ArtifactStore | None = None,
        history_limit: int = _DEFAULT_HISTORY_LIMIT,
    ) -> None:
        if not 1 <= max_output_bytes <= _DEFAULT_OUTPUT_LIMIT:
            raise ValueError(
                f"max_output_bytes must be between 1 and {_DEFAULT_OUTPUT_LIMIT}"
            )
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self._bus = bus
        self._jobs: dict[str, BackgroundJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._ready: dict[str, asyncio.Event] = {}
        self._process_supervisor = process_supervisor or ProcessSupervisor()
        self._max_output_bytes = max_output_bytes
        self._artifact_store = artifact_store
        self._history_limit = history_limit

    # 淘汰最旧的已完成任务记录和 asyncio Task，限制 daemon 长期运行的内存增长
    def _prune_finished(self) -> None:
        overflow = len(self._jobs) - self._history_limit + 1
        if overflow <= 0:
            return
        candidates = sorted(
            (
                job
                for job in self._jobs.values()
                if (task := self._tasks.get(job.id)) is not None and task.done()
            ),
            key=lambda job: job.created_at,
        )
        for job in candidates[:overflow]:
            self._jobs.pop(job.id, None)
            self._tasks.pop(job.id, None)
            self._ready.pop(job.id, None)

    # 启动后台 shell 任务并立即返回可查询的任务记录
    def start(
        self,
        command: str,
        timeout: int,
        session_id: str,
        run_id: str,
        sandbox_plan: SandboxPlan | None = None,
        cwd: Path | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> BackgroundJob:
        self._prune_finished()
        job = BackgroundJob(
            id=f"bg-{uuid.uuid4().hex[:12]}",
            command=command,
            session_id=session_id,
            run_id=run_id,
            created_at=_now(),
            sandbox_backend=(
                sandbox_plan.backend if sandbox_plan is not None else "degraded"
            ),
            cwd=str(cwd.resolve()) if cwd is not None else "",
        )
        self._jobs[job.id] = job
        self._ready[job.id] = asyncio.Event()
        self._tasks[job.id] = asyncio.create_task(
            self._execute(
                job,
                timeout,
                sandbox_plan,
                cwd,
                artifact_store or self._artifact_store,
            ),
            name=f"background:{job.id}",
        )
        return job

    # 执行后台命令、保存结果并发布开始和结束事件
    async def _execute(
        self,
        job: BackgroundJob,
        timeout: int,
        sandbox_plan: SandboxPlan | None,
        cwd: Path | None,
        artifact_store: ArtifactStore | None,
    ) -> None:
        output_task: asyncio.Task[None] | None = None
        output_ring = _OutputRing(self._max_output_bytes)
        await self._bus.publish(
            BackgroundJobStartedEvent(
                job_id=job.id,
                run_id=job.run_id,
                session_id=job.session_id,
                command=job.command,
                ts=_now(),
            )
        )
        output_spool = ArtifactSpool(artifact_store)
        try:
            process = await spawn_sandboxed_shell(
                sandbox_plan,
                SandboxSpawnRequest(
                    command=job.command,
                    label=f"background:{job.id}",
                    cwd=cwd,
                    interactive_stdin=True,
                ),
                self._process_supervisor,
            )
            self._processes[job.id] = process
            self._ready[job.id].set()
            assert process.stdout is not None
            output_task = asyncio.create_task(
                self._stream_output(job, process.stdout, output_ring, output_spool),
                name=f"background-output:{job.id}",
            )
            try:
                await asyncio.wait_for(
                    wait_for_process_leader(process),
                    timeout=timeout,
                )
            except TimeoutError:
                job.process_usage = (
                    await self._process_supervisor.terminate(process)
                ).to_dict()
                await self._join_output_reader(output_task)
                captured = output_ring.render()
                job.output = f"[timeout after {timeout}s]"
                if captured:
                    job.output += f"\n{captured}"
                job.status = "failed"
                job.is_error = True
            else:
                job.process_usage = (
                    await self._process_supervisor.terminate(process)
                ).to_dict()
                await self._join_output_reader(output_task)
                output = output_ring.render()
                return_code = process.returncode or 0
                job.output = output or "[no output]"
                job.is_error = return_code != 0
                job.status = "failed" if job.is_error else "completed"
                if job.is_error:
                    job.output = f"[exit {return_code}]\n{job.output}"
        except asyncio.CancelledError:
            active_process = self._processes.get(job.id)
            if active_process is not None:
                usage = await asyncio.shield(
                    self._process_supervisor.terminate(active_process)
                )
                job.process_usage = usage.to_dict()
            if output_task is not None:
                await asyncio.shield(self._join_output_reader(output_task))
            job.status = "cancelled"
            captured = output_ring.render()
            job.output = "Background job cancelled."
            if captured:
                job.output += f"\n{captured}"
            job.is_error = True
        except Exception as exc:
            job.status = "failed"
            job.output = str(exc)
            job.is_error = True
        finally:
            active_process = self._processes.pop(job.id, None)
            if active_process is not None and job.process_usage is None:
                if active_process.returncode is None:
                    job.process_usage = (
                        await self._process_supervisor.terminate(active_process)
                    ).to_dict()
                else:
                    job.process_usage = self._process_supervisor.forget(
                        active_process
                    ).to_dict()
            if output_task is not None and not output_task.done():
                await self._join_output_reader(output_task)
            try:
                artifact = await output_spool.finish(persist=output_ring.truncated)
            except (ArtifactError, OSError) as exc:
                job.output_artifact_error = getattr(exc, "code", "artifact_error")
                if output_ring.truncated:
                    job.output += "\n[full output artifact unavailable]"
            else:
                if artifact is not None:
                    job.output_artifact = artifact.handle
                    job.output_artifact_size = artifact.size
                    job.output += (
                        f"\n[full output: {artifact.handle}; size={artifact.size}; "
                        "use artifact_read with offset/limit]"
                    )
            self._ready[job.id].set()
            job.finished_at = _now()
            await self._bus.publish(
                BackgroundJobFinishedEvent(
                    job_id=job.id,
                    run_id=job.run_id,
                    session_id=job.session_id,
                    status=job.status,
                    output_preview=job.output[:500],
                    process_usage=job.process_usage or {},
                    ts=job.finished_at,
                )
            )

    # 持续分块读取 stdout/stderr 合流并实时刷新任务的有界输出快照
    async def _stream_output(
        self,
        job: BackgroundJob,
        stream: asyncio.StreamReader,
        output_ring: _OutputRing,
        output_spool: ArtifactSpool,
    ) -> None:
        while True:
            chunk = await stream.read(_READ_CHUNK_SIZE)
            if not chunk:
                return
            output_spool.write(chunk)
            output_ring.append(chunk)
            job.output_bytes = output_ring.total_bytes
            job.output_truncated = output_ring.truncated
            job.output = output_ring.render()

    # 有界等待输出 reader 收到 EOF，避免遗留子进程持有管道导致任务永久卡住
    async def _join_output_reader(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # 返回指定后台任务，不存在时返回 None
    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(job_id)

    # 仅在任务属于指定非空 session 时返回记录，避免跨会话探测任务 ID
    def get_for_session(self, job_id: str, session_id: str) -> BackgroundJob | None:
        if not session_id:
            return None
        job = self._jobs.get(job_id)
        if job is None or job.session_id != session_id:
            return None
        return job

    # 返回指定 session 或全部后台任务的快照
    def list(self, session_id: str = "") -> list[BackgroundJob]:
        jobs = list(self._jobs.values())
        if session_id:
            jobs = [job for job in jobs if job.session_id == session_id]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    # 等待指定任务到终态或达到本次轮询超时，并返回最新记录
    async def wait(
        self,
        job_id: str,
        timeout: float,
        *,
        session_id: str | None = None,
    ) -> BackgroundJob | None:
        job = (
            self.get_for_session(job_id, session_id)
            if session_id is not None
            else self._jobs.get(job_id)
        )
        task = self._tasks.get(job_id)
        if job is None or task is None:
            return None
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except TimeoutError:
                pass
        return job

    # 向运行中的后台 shell 写入 stdin，并可按请求关闭输入流
    async def interact(
        self,
        job_id: str,
        data: str,
        *,
        close_stdin: bool,
        session_id: str | None = None,
    ) -> bool:
        if session_id is not None and self.get_for_session(job_id, session_id) is None:
            return False
        ready = self._ready.get(job_id)
        if ready is None:
            return False
        try:
            await asyncio.wait_for(ready.wait(), timeout=2.0)
        except TimeoutError:
            return False
        process = self._processes.get(job_id)
        if process is None or process.returncode is not None or process.stdin is None:
            return False
        if data:
            process.stdin.write(data.encode("utf-8"))
            await process.stdin.drain()
        if close_stdin:
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
        return True

    # 取消仍在运行的后台任务并等待子进程完成清理
    async def cancel(self, job_id: str, *, session_id: str | None = None) -> bool:
        if session_id is not None and self.get_for_session(job_id, session_id) is None:
            return False
        task = self._tasks.get(job_id)
        if task is None or task.done():
            return False
        job = self._jobs[job_id]
        job.status = "cancelled"
        job.output = "Background job cancelled."
        job.is_error = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if not job.finished_at:
            job.finished_at = _now()
            await self._bus.publish(
                BackgroundJobFinishedEvent(
                    job_id=job.id,
                    run_id=job.run_id,
                    session_id=job.session_id,
                    status=job.status,
                    output_preview=job.output,
                    process_usage=job.process_usage or {},
                    ts=job.finished_at,
                )
            )
        return True

    # 取消并等待全部后台任务，用于 daemon 退出
    async def cancel_all(self) -> None:
        active = [task for task in self._tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

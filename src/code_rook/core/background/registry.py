from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from code_rook.core.bus.events import BackgroundJobFinishedEvent, BackgroundJobStartedEvent
from code_rook.core.events.bus import EventBus


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


class BackgroundJobRegistry:
    # 初始化 daemon 级后台任务表和事件总线
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._jobs: dict[str, BackgroundJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # 启动后台 shell 任务并立即返回可查询的任务记录
    def start(self, command: str, timeout: int, session_id: str, run_id: str) -> BackgroundJob:
        job = BackgroundJob(
            id=f"bg-{uuid.uuid4().hex[:12]}",
            command=command,
            session_id=session_id,
            run_id=run_id,
            created_at=_now(),
        )
        self._jobs[job.id] = job
        self._tasks[job.id] = asyncio.create_task(
            self._execute(job, timeout),
            name=f"background:{job.id}",
        )
        return job

    # 执行后台命令、保存结果并发布开始和结束事件
    async def _execute(self, job: BackgroundJob, timeout: int) -> None:
        from code_rook.core.tools.builtin.bash import BashTool

        await self._bus.publish(
            BackgroundJobStartedEvent(
                job_id=job.id,
                run_id=job.run_id,
                session_id=job.session_id,
                command=job.command,
                ts=_now(),
            )
        )
        try:
            result = await BashTool().invoke({"command": job.command, "timeout": timeout})
            job.output = result.content
            job.is_error = result.is_error
            job.status = "failed" if result.is_error else "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.output = "Background job cancelled."
            job.is_error = True
        finally:
            job.finished_at = _now()
            await self._bus.publish(
                BackgroundJobFinishedEvent(
                    job_id=job.id,
                    run_id=job.run_id,
                    session_id=job.session_id,
                    status=job.status,
                    output_preview=job.output[:500],
                    ts=job.finished_at,
                )
            )

    # 返回指定后台任务，不存在时返回 None
    def get(self, job_id: str) -> BackgroundJob | None:
        return self._jobs.get(job_id)

    # 返回指定 session 或全部后台任务的快照
    def list(self, session_id: str = "") -> list[BackgroundJob]:
        jobs = list(self._jobs.values())
        if session_id:
            jobs = [job for job in jobs if job.session_id == session_id]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    # 取消仍在运行的后台任务并等待子进程完成清理
    async def cancel(self, job_id: str) -> bool:
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

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.redaction import minimize_trace_data, redact_trace_data

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceWriterStatus:
    degraded: bool
    running: bool
    queue_size: int
    queue_capacity: int
    dropped_records: int
    last_error: str


class TraceWriter:
    # 初始化 TraceWriter；写入目标文件路径在 start() 前不会创建
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        include_payload: bool = False,
        queue_size: int = 1024,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if backup_count < 0:
            raise ValueError("backup_count must be non-negative")
        if queue_size < 1:
            raise ValueError("trace queue_size must be positive")
        self._path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._include_payload = include_payload
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._degraded = False
        self._dropped_records = 0
        self._last_error = ""

    # 创建目录、启动后台 drain task
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._drain())

    # 等待队列清空后取消 drain task
    async def stop(self) -> None:
        await self._queue.join()
        if self._task is not None:
            task = self._task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._task = None

    # 返回当前有界队列、丢弃计数和最后写入错误的可观测快照
    def status(self) -> TraceWriterStatus:
        self._observe_task_failure()
        return TraceWriterStatus(
            degraded=self._degraded,
            running=self._task is not None and not self._task.done(),
            queue_size=self._queue.qsize(),
            queue_capacity=self._queue.maxsize,
            dropped_records=self._dropped_records,
            last_error=self._last_error,
        )

    # 非阻塞地放入有界队列；满载或 writer 失败时丢弃并返回 False
    def emit(self, record: TraceRecord) -> bool:
        self._observe_task_failure()
        if self._task is not None and self._task.done():
            self._drop("trace writer task is not running")
            return False
        data = record.data
        if not self._include_payload and record.layer != "llm":
            data = minimize_trace_data(record.layer, record.kind, data)
        safe_record = record.model_copy(
            update={"data": redact_trace_data(data)}
        )
        try:
            self._queue.put_nowait(safe_record)
        except asyncio.QueueFull:
            self._drop("trace queue is full; record dropped")
            return False
        return True

    # 持续从队列读取 record 并追加写入文件
    async def _drain(self) -> None:
        file = None
        try:
            file = self._path.open("ab")
            current_size = self._path.stat().st_size
            while True:
                record = await self._queue.get()
                try:
                    encoded = record.model_dump_json().encode("utf-8") + b"\n"
                    if (
                        self._max_bytes > 0
                        and current_size > 0
                        and current_size + len(encoded) > self._max_bytes
                    ):
                        file.close()
                        self._rotate()
                        file = self._path.open("ab")
                        current_size = 0
                    file.write(encoded)
                    file.flush()
                    current_size += len(encoded)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._mark_degraded(f"{type(exc).__name__}: {exc}")
            while True:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    self._queue.task_done()
            raise
        finally:
            if file is not None:
                file.close()

    # 记录一次丢弃并只在首次进入降级状态时输出错误日志
    def _drop(self, reason: str) -> None:
        self._dropped_records += 1
        self._mark_degraded(reason)

    # 将 writer 标记为降级并保存最后错误，供诊断面查询
    def _mark_degraded(self, reason: str) -> None:
        first_failure = not self._degraded
        self._degraded = True
        self._last_error = reason
        if first_failure:
            logger.error("TraceWriter degraded: %s", reason)

    # 观察已终止后台 task 的异常并同步到降级状态
    def _observe_task_failure(self) -> None:
        if self._task is None or not self._task.done() or self._task.cancelled():
            return
        error = self._task.exception()
        if error is not None:
            self._mark_degraded(f"{type(error).__name__}: {error}")

    def _rotate(self) -> None:
        if not self._path.exists():
            return
        if self._backup_count == 0:
            self._path.unlink()
            return
        oldest = self._backup_path(self._backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                os.replace(source, self._backup_path(index + 1))
        os.replace(self._path, self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")

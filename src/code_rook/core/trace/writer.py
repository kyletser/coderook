from __future__ import annotations

import asyncio
import os
from pathlib import Path

from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.redaction import minimize_trace_data, redact_trace_data


class TraceWriter:
    # 初始化 TraceWriter；写入目标文件路径在 start() 前不会创建
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
        include_payload: bool = False,
    ) -> None:
        if max_bytes < 0:
            raise ValueError("max_bytes must be non-negative")
        if backup_count < 0:
            raise ValueError("backup_count must be non-negative")
        self._path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._include_payload = include_payload
        self._queue: asyncio.Queue[TraceRecord] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

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
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # 非阻塞地将 record 放入写入队列
    def emit(self, record: TraceRecord) -> None:
        data = record.data
        if not self._include_payload and record.layer != "llm":
            data = minimize_trace_data(record.layer, record.kind, data)
        safe_record = record.model_copy(
            update={"data": redact_trace_data(data)}
        )
        self._queue.put_nowait(safe_record)

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
        except BaseException:
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

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from kyle_claude.core.context import ExecutionContext


@dataclass
class _BackgroundEntry:
    task: asyncio.Task[None]
    context: ExecutionContext
    parent_run_id: str


# 管理后台 subagent 任务的生命周期：注册、查询、批量取消
class BackgroundTaskRegistry:
    # 初始化 daemon 级任务表，并限制已完成结果的内存保留数量
    def __init__(self, max_entries: int = 256) -> None:
        self._tasks: dict[str, _BackgroundEntry] = {}
        self._max_entries = max(1, max_entries)

    # 注册一个后台任务及其执行上下文
    def register(
        self,
        run_id: str,
        task: asyncio.Task[None],
        context: ExecutionContext,
        parent_run_id: str = "",
    ) -> None:
        self._tasks[run_id] = _BackgroundEntry(task, context, parent_run_id)
        task.add_done_callback(lambda _task: self._prune_completed())

    # 超过容量时按注册顺序淘汰最早的已完成任务，运行中的任务永不被自动移除
    def _prune_completed(self) -> None:
        for run_id, entry in list(self._tasks.items()):
            if len(self._tasks) <= self._max_entries:
                break
            if entry.task.done():
                self._tasks.pop(run_id, None)

    # 查询后台任务及其上下文；不存在时返回 None
    def get(self, run_id: str) -> tuple[asyncio.Task[None], ExecutionContext] | None:
        entry = self._tasks.get(run_id)
        return (entry.task, entry.context) if entry is not None else None

    # 返回所有已注册的 (task, context) 对，用于 daemon 退出时批量清理
    def all(self) -> list[tuple[asyncio.Task[None], ExecutionContext]]:
        return [(entry.task, entry.context) for entry in self._tasks.values()]

    async def cancel_descendants(self, parent_run_id: str) -> None:
        descendant_ids: set[str] = set()
        frontier = {parent_run_id}
        while frontier:
            children = {
                run_id
                for run_id, entry in self._tasks.items()
                if entry.parent_run_id in frontier and run_id not in descendant_ids
            }
            descendant_ids.update(children)
            frontier = children
        entries = [self._tasks[run_id] for run_id in descendant_ids]
        for entry in entries:
            if not entry.context.is_done():
                entry.context.mark_failed("cancelled")
            if not entry.task.done():
                entry.task.cancel()
        if entries:
            await asyncio.gather(*(entry.task for entry in entries), return_exceptions=True)

    async def cancel_all(self) -> None:
        entries = list(self._tasks.values())
        for entry in entries:
            if not entry.context.is_done():
                entry.context.mark_failed("cancelled")
            if not entry.task.done():
                entry.task.cancel()
        if entries:
            await asyncio.gather(*(entry.task for entry in entries), return_exceptions=True)

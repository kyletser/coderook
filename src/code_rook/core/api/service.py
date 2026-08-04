from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, cast

import code_rook
from code_rook.core.authority import RuntimeMode
from code_rook.core.receipts.models import TurnReceipt
from code_rook.core.runs import new_run_id
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    ThreadRecord,
    TurnItemRecord,
    TurnRecord,
)
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RecordNotFoundError
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.model import SessionMode

logger = logging.getLogger(__name__)


class RuntimeApiService:
    # 初始化统一 runtime API facade，所有写操作复用 SessionManager 状态机
    def __init__(self, runtime: RuntimeService, sessions: SessionManager) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._tasks: set[asyncio.Task[Any]] = set()

    # 跟踪 API 启动的后台 turn 并记录未被读取的异常
    def _track(self, coroutine: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)

        # 移除完成任务并消费异常，避免事件循环输出未读取警告
        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.error("API background turn failed: %s", error)

        task.add_done_callback(finished)
        return task

    # 列出所有 durable threads
    async def list_threads(self) -> list[ThreadRecord]:
        return await self._runtime.list_threads()

    # 创建 chat thread 并返回其 durable 投影
    async def create_thread(self, title: str, mode: str) -> ThreadRecord:
        session = await self._sessions.create(mode=cast(SessionMode, mode), title=title)
        return await self._runtime.get_thread(session.id)

    # 验证 thread 存在，供长连接在发送 200 header 前失败
    async def ensure_thread(self, thread_id: str) -> None:
        await self._runtime.get_thread(thread_id)

    # 启动后台 turn，并等待其 durable running 记录对读接口可见
    async def create_turn(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode,
    ) -> TurnRecord:
        run_id = new_run_id()
        task = self._track(
            self._sessions.send_message(
                thread_id,
                content,
                run_id=run_id,
                runtime_mode=mode,
            ),
            name=f"api-turn:{run_id}",
        )
        for _ in range(2_000):
            try:
                return await self._runtime.get_turn(run_id)
            except RecordNotFoundError:
                if task.done():
                    await task
                await asyncio.sleep(0.001)
        raise TimeoutError("turn did not enter durable runtime within two seconds")

    # 中断当前活动 turn
    async def interrupt_turn(self, turn_id: str) -> TurnRecord:
        await self._sessions.cancel_run(turn_id)
        return await self._runtime.get_turn(turn_id)

    # 向当前活动 turn 注入用户 steering 指令
    async def steer_turn(self, turn_id: str, content: str) -> TurnRecord:
        await self._sessions.steer_run(turn_id, content)
        return await self._runtime.get_turn(turn_id)

    # 读取 thread 的 durable 事件游标窗口
    async def list_events(
        self,
        thread_id: str,
        after_seq: int,
        limit: int = 1000,
    ) -> list[RuntimeEventRecord]:
        return await self._runtime.list_events(thread_id, after_seq=after_seq, limit=limit)

    # 读取 turn 的 durable items
    async def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        await self._runtime.get_turn(turn_id)
        return await self._runtime.list_items(turn_id)

    # 读取 turn 的 durable receipt
    async def get_receipt(self, turn_id: str) -> TurnReceipt:
        return await self._runtime.get_receipt(turn_id)

    # 返回服务端可协商的 API 和运行能力
    async def capabilities(self) -> dict[str, Any]:
        return {
            "version": code_rook.__version__,
            "api_version": "v1",
            "runtime_modes": [mode.value for mode in RuntimeMode],
            "features": [
                "durable_threads",
                "durable_turns",
                "sse_cursor_replay",
                "turn_receipts",
                "interrupt",
                "steer",
            ],
        }

    # 汇总全部 durable turns 的 token usage 与状态计数
    async def usage(self) -> dict[str, Any]:
        totals: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        turn_count = 0
        threads = await self._runtime.list_threads()
        for thread in threads:
            for turn in await self._runtime.list_turns(thread.id):
                turn_count += 1
                status_counts[turn.status.value] = status_counts.get(turn.status.value, 0) + 1
                for key, value in turn.usage.items():
                    if key.endswith("tokens") and isinstance(value, (int, float)):
                        totals[key] = totals.get(key, 0) + int(value)
        return {
            "threads": len(threads),
            "turns": turn_count,
            "status_counts": status_counts,
            "tokens": totals,
            "cost": "unknown",
        }

    # 等待 API 启动的后台 turn 结束，仅用于受控关闭与测试
    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

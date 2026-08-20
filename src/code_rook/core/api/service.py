from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Coroutine
from typing import Any, cast

import code_rook
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.events import RuntimeEventAppendedEvent
from code_rook.core.compatibility import (
    HTTP_API_VERSION,
    RUNTIME_EVENT_SCHEMA_VERSION,
    STREAM_JSON_SCHEMA_VERSIONS,
)
from code_rook.core.permissions.manager import PermissionManager
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
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.workspace import WorkspaceBoundary

logger = logging.getLogger(__name__)
_TURN_DURABILITY_TIMEOUT_S = 10.0


class RuntimeApiService:
    # 初始化统一 runtime API facade，所有写操作复用 SessionManager 状态机
    def __init__(
        self,
        runtime: RuntimeService,
        sessions: SessionManager,
        *,
        permission_manager: PermissionManager | None = None,
        workspace_boundary: WorkspaceBoundary | None = None,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._tasks: set[asyncio.Task[Any]] = set()
        self._event_changed = asyncio.Condition()
        self._permission_manager = permission_manager
        self._git_diff = GitDiffTool(workspace_boundary) if workspace_boundary else None

    # 响应活动工具审批并返回是否命中待处理请求
    async def respond_permission(
        self,
        tool_use_id: str,
        decision: str,
        *,
        selected_hunks: list[str] | None = None,
        patch_plan_id: str | None = None,
    ) -> dict[str, object]:
        if self._permission_manager is None:
            raise ValueError("permission control is unavailable")
        accepted = self._permission_manager.respond(
            tool_use_id,
            decision,
            selected_hunks=selected_hunks,
            patch_plan_id=patch_plan_id,
        )
        return {"tool_use_id": tool_use_id, "accepted": accepted}

    # 读取工作区结构化 diff，供 IDE 等 HTTP 客户端打开变更视图
    async def workspace_diff(
        self,
        *,
        scope: str = "all",
        path: str = ".",
    ) -> dict[str, object]:
        if self._git_diff is None:
            raise ValueError("workspace diff is unavailable")
        result = await self._git_diff.invoke({"scope": scope, "path": path})
        payload = json.loads(result.content)
        if not isinstance(payload, dict):
            raise ValueError("workspace diff returned an invalid payload")
        return cast(dict[str, object], payload)

    # 作为 EventBus 订阅者：新的 durable 事件落盘后唤醒全部等待者
    async def notify_runtime_event(self, event: Any) -> None:
        if not isinstance(event, RuntimeEventAppendedEvent):
            return
        async with self._event_changed:
            self._event_changed.notify_all()

    # 挂起等待新的 runtime 事件通知，超时自动返回（供 SSE 与 create_turn 使用）
    async def wait_for_change(self, timeout: float) -> None:
        async with self._event_changed:
            try:
                await asyncio.wait_for(self._event_changed.wait(), timeout=timeout)
            except TimeoutError:
                return

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

    # 读取单个 durable thread
    async def get_thread(self, thread_id: str) -> ThreadRecord:
        return await self._runtime.get_thread(thread_id)

    # 通过 SessionManager 更新标题或归档状态并返回 durable 投影
    async def update_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ThreadRecord:
        if title is not None:
            await self._sessions.rename(thread_id, title)
        if archived is True:
            await self._sessions.close(thread_id)
        elif archived is False:
            raise ValueError("unarchiving is not supported by the current session ledger")
        return await self._runtime.get_thread(thread_id)

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
        deadline = time.monotonic() + _TURN_DURABILITY_TIMEOUT_S
        while True:
            try:
                return await self._runtime.get_turn(run_id)
            except RecordNotFoundError:
                if task.done():
                    await task
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await self.wait_for_change(min(remaining, 0.1))
        raise TimeoutError(
            "turn did not enter durable runtime within "
            f"{_TURN_DURABILITY_TIMEOUT_S:g} seconds"
        )

    # 列出指定 thread 的 durable turns
    async def list_turns(self, thread_id: str) -> list[TurnRecord]:
        await self._runtime.get_thread(thread_id)
        return await self._runtime.list_turns(thread_id)

    # 读取单个 durable turn
    async def get_turn(self, turn_id: str) -> TurnRecord:
        return await self._runtime.get_turn(turn_id)

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
            "api_version": HTTP_API_VERSION,
            "runtime_event_schema_version": RUNTIME_EVENT_SCHEMA_VERSION,
            "stream_json_schema_versions": list(STREAM_JSON_SCHEMA_VERSIONS),
            "runtime_modes": [mode.value for mode in RuntimeMode],
            "features": [
                "durable_threads",
                "durable_turns",
                "sse_cursor_replay",
                "turn_receipts",
                "interrupt",
                "steer",
                "permission_response",
                "workspace_diff",
            ],
        }

    # 汇总全部 durable turns 的 token usage 与状态计数
    async def usage(self) -> dict[str, Any]:
        totals: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        turn_count = 0
        estimated_cost_usd = 0.0
        cost_known = True
        threads = await self._runtime.list_threads()
        for thread in threads:
            for turn in await self._runtime.list_turns(thread.id):
                turn_count += 1
                status_counts[turn.status.value] = status_counts.get(turn.status.value, 0) + 1
                for key, value in turn.usage.items():
                    if key.endswith("tokens") and isinstance(value, (int, float)):
                        totals[key] = totals.get(key, 0) + int(value)
                turn_cost = turn.usage.get("estimated_cost_usd")
                if isinstance(turn_cost, (int, float)):
                    estimated_cost_usd += float(turn_cost)
                elif turn.usage:
                    cost_known = False
        return {
            "threads": len(threads),
            "turns": turn_count,
            "status_counts": status_counts,
            "tokens": totals,
            "cost": estimated_cost_usd if cost_known else "unknown",
        }

    # 等待 API 启动的后台 turn 结束，仅用于受控关闭与测试
    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

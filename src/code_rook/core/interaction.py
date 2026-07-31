from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime

from code_rook.core.events.bus import EventBus


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _PendingQuestion:
    future: asyncio.Future[str]
    run_id: str
    session_id: str


class InteractionManager:
    # 初始化问题等待表、活动 run 集合和逐 run 纠偏队列
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._pending_questions: dict[str, _PendingQuestion] = {}
        self._active_runs: set[str] = set()
        self._steering: dict[str, deque[str]] = defaultdict(deque)

    # 注册一个可接受运行中纠偏的活动 run
    def register_run(self, run_id: str) -> None:
        self._active_runs.add(run_id)

    # 注销活动 run 并清理未消费纠偏和待回答问题
    def unregister_run(self, run_id: str) -> None:
        self._active_runs.discard(run_id)
        self._steering.pop(run_id, None)
        for question_id, pending in list(self._pending_questions.items()):
            if pending.run_id != run_id:
                continue
            self._pending_questions.pop(question_id, None)
            if not pending.future.done():
                pending.future.cancel()

    # 将用户纠偏加入活动 run 队列，不存在或已结束时返回 false
    def steer(self, run_id: str, content: str) -> bool:
        normalized = content.strip()
        if run_id not in self._active_runs or not normalized:
            return False
        self._steering[run_id].append(normalized)
        return True

    # 按到达顺序取走指定 run 当前全部纠偏消息
    def drain_steering(self, run_id: str) -> list[str]:
        queued = self._steering.pop(run_id, deque())
        return list(queued)

    # 发布结构化用户问题并挂起工具调用直到客户端回答
    async def ask(
        self,
        *,
        run_id: str,
        session_id: str,
        question: str,
        header: str,
        options: list[str],
        multi_select: bool,
    ) -> str:
        from code_rook.core.bus.events import UserQuestionAskedEvent

        question_id = f"question-{uuid.uuid4().hex[:12]}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_questions[question_id] = _PendingQuestion(
            future=future,
            run_id=run_id,
            session_id=session_id,
        )
        await self._bus.publish(
            UserQuestionAskedEvent(
                question_id=question_id,
                run_id=run_id,
                session_id=session_id,
                question=question,
                header=header,
                options=options,
                multi_select=multi_select,
                ts=_now(),
            )
        )
        try:
            return await future
        finally:
            self._pending_questions.pop(question_id, None)

    # 用用户答案解决指定结构化问题，未知问题返回 false
    def answer(self, question_id: str, answer: str) -> bool:
        pending = self._pending_questions.get(question_id)
        normalized = answer.strip()
        if pending is None or pending.future.done() or not normalized:
            return False
        pending.future.set_result(normalized)
        return True

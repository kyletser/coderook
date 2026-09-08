from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from code_rook.core.events.bus import EventBus


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class _PendingQuestion:
    future: asyncio.Future[str]
    run_id: str
    session_id: str


@dataclass(frozen=True)
class HeadlessQuestionPolicy:
    mode: str = "fail_fast"
    timeout_s: float | None = None
    answers: tuple[str, ...] = ()


class HeadlessQuestionRequiredError(RuntimeError):
    pass


@dataclass(frozen=True)
class FollowUpMessage:
    content: UserMessageContent
    admitted: Callable[[], Awaitable[None]]


UserMessageContent = str | list[dict[str, Any]] | dict[str, Any]
ExtensionMessageSender = Callable[
    [UserMessageContent, Literal["steer", "follow_up"], bool], Awaitable[None]
]


class InteractionManager:
    # 初始化问题等待表、活动 run 集合和逐 run 纠偏队列
    def __init__(
        self, bus: EventBus, *, steering_mode: Literal["one-at-a-time", "all"] = "one-at-a-time"
    ) -> None:
        self._bus = bus
        self._steering_mode = steering_mode
        self._pending_questions: dict[str, _PendingQuestion] = {}
        self._active_runs: set[str] = set()
        self._steering: dict[str, deque[UserMessageContent]] = defaultdict(deque)
        self._question_policies: dict[str, HeadlessQuestionPolicy] = {}
        self._preset_answers: dict[str, deque[str]] = {}
        self._follow_up_sources: dict[str, Callable[[], Awaitable[list[FollowUpMessage]]]] = {}
        self._extension_senders: dict[str, ExtensionMessageSender] = {}
        self._extension_followups: dict[str, deque[dict[str, Any]]] = defaultdict(deque)

    # 更新纠偏队列的交付模式，不丢弃已排队消息
    def set_steering_mode(self, mode: Literal["one-at-a-time", "all"]) -> None:
        self._steering_mode = mode

    # 为 headless session 设置有限等待或预置答案策略
    def set_question_policy(
        self,
        session_id: str,
        policy: HeadlessQuestionPolicy,
    ) -> None:
        self._question_policies[session_id] = policy
        self._preset_answers[session_id] = deque(policy.answers)

    # 清除 headless session 的临时提问策略
    def clear_question_policy(self, session_id: str) -> None:
        self._question_policies.pop(session_id, None)
        self._preset_answers.pop(session_id, None)

    # 注册一个可接受运行中纠偏的活动 run
    def register_run(self, run_id: str) -> None:
        self._active_runs.add(run_id)

    # 注销活动 run 并清理未消费纠偏和待回答问题
    def unregister_run(self, run_id: str) -> None:
        self._active_runs.discard(run_id)
        self._steering.pop(run_id, None)
        self._follow_up_sources.pop(run_id, None)
        self._extension_senders.pop(run_id, None)
        self._extension_followups.pop(run_id, None)
        for question_id, pending in list(self._pending_questions.items()):
            if pending.run_id != run_id:
                continue
            self._pending_questions.pop(question_id, None)
            if not pending.future.done():
                pending.future.cancel()

    # 将用户纠偏加入活动 run 队列，不存在或已结束时返回 false
    def steer(self, run_id: str, content: UserMessageContent) -> bool:
        normalized = content.strip() if isinstance(content, str) else deepcopy(content)
        if run_id not in self._active_runs or not normalized:
            return False
        self._steering[run_id].append(normalized)
        return True

    # 默认每轮只消费一条纠偏，显式 all 模式保留批量交付语义。
    def drain_steering(self, run_id: str) -> list[UserMessageContent]:
        queued = self._steering.get(run_id)
        if not queued:
            return []
        if self._steering_mode == "all":
            return list(self._steering.pop(run_id))
        message = queued.popleft()
        if not queued:
            self._steering.pop(run_id, None)
        return [message]

    # 将会话持久队列的消费入口绑定到本次运行，只有外层循环空闲时调用。
    def bind_follow_up(
        self, run_id: str, source: Callable[[], Awaitable[list[FollowUpMessage]]]
    ) -> None:
        self._follow_up_sources[run_id] = source

    # 释放会话绑定，即使 runner 在正式注册运行前失败也不残留闭包。
    def unbind_follow_up(self, run_id: str) -> None:
        self._follow_up_sources.pop(run_id, None)
        self._extension_senders.pop(run_id, None)

    # 绑定会话拥有的消息提交入口，扩展不直接操作持久队列
    def bind_extension_sender(self, run_id: str, sender: ExtensionMessageSender) -> None:
        self._extension_senders[run_id] = sender

    # 将扩展消息送往当前会话的纠偏或后续队列
    async def send_extension_message(
        self, run_id: str, content: UserMessageContent, deliver_as: Literal["steer", "follow_up"],
        expand_prompt_templates: bool,
    ) -> None:
        sender = self._extension_senders.get(run_id)
        if sender is None:
            raise RuntimeError("Extension messaging requires an active session")
        await sender(content, deliver_as, expand_prompt_templates)

    # 在当前回答完成后按会话提供的策略领取后续消息。
    async def drain_follow_up(self, run_id: str) -> list[FollowUpMessage]:
        extension = self._extension_followups.get(run_id)
        if extension:
            content = extension.popleft()
            if not extension:
                self._extension_followups.pop(run_id, None)

            # 扩展自定义消息在循环持久化后无需额外确认持久队列。
            async def admitted() -> None:
                return None

            return [FollowUpMessage(content, admitted)]
        source = self._follow_up_sources.get(run_id)
        return await source() if source is not None else []

    # 将扩展自定义消息排到当前回答结束后的独立 Turn。
    def queue_extension_follow_up(self, run_id: str, message: dict[str, Any]) -> bool:
        if run_id not in self._active_runs or message.get("role") != "custom":
            return False
        self._extension_followups[run_id].append(deepcopy(message))
        return True

    # 停止时取回尚未送入模型的全部纠偏，供会话层保存为可恢复草稿。
    def take_pending_steering(self, run_id: str) -> list[UserMessageContent]:
        return list(self._steering.pop(run_id, deque()))

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
            policy = self._question_policies.get(session_id)
            if policy is None:
                return await future
            if policy.mode == "preset":
                answers = self._preset_answers.get(session_id)
                if answers:
                    return answers.popleft()
                raise HeadlessQuestionRequiredError(
                    "headless preset answers were exhausted"
                )
            if policy.mode == "timeout" and policy.timeout_s is not None:
                try:
                    return await asyncio.wait_for(future, timeout=policy.timeout_s)
                except TimeoutError as exc:
                    raise HeadlessQuestionRequiredError(
                        f"headless question timed out after {policy.timeout_s:g}s"
                    ) from exc
            raise HeadlessQuestionRequiredError(
                "headless question requires input; configure timeout or preset answers"
            )
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

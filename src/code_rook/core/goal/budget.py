from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING

from pydantic import BaseModel

from code_rook.core.bus.events import LlmUsageEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.budget import output_token_budget
from code_rook.core.llm.types import LlmResponse

if TYPE_CHECKING:
    from code_rook.core.goal.service import GoalService
    from code_rook.core.llm.base import LLMProvider

logger = logging.getLogger(__name__)
_MAX_MODEL_OUTPUT_TOKENS = 24_576


class GoalBudgetError(RuntimeError):
    reason = "goal_budget_error"


class GoalTokenBudgetExhausted(GoalBudgetError):
    reason = "token_budget_exhausted"


class GoalTokenBudgetReserved(GoalBudgetError):
    reason = "token_budget_reserved"


class GoalTokenUsageUnavailable(GoalBudgetError):
    reason = "token_usage_unavailable"


# 估算请求输入 token 的保守上界，以 UTF-8 字节数避免并发 lease 低估输入
def _estimate_request_tokens(
    messages: list[dict[str, object]],
    tool_schemas: list[dict[str, object]],
    system: str | None,
) -> int:
    rendered = json.dumps(
        {
            "messages": messages,
            "tools": tool_schemas,
            "system": system or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    structural_overhead = 64 * (len(messages) + len(tool_schemas) + 1)
    return max(1, len(rendered.encode("utf-8")) + structural_overhead)


class _GoalUsageBus(EventBus):
    # 包装调用方事件总线，累计真实 usage 并保持原始事件链不变
    def __init__(self, inner: EventBus) -> None:
        super().__init__()
        self._inner = inner
        self.usage_events = 0
        self.actual_tokens = 0

    # 捕获 provider usage 供 lease 结算，同时保留 TUI、Runtime 和 Trace 的原始事件链
    async def publish(self, event: BaseModel) -> None:
        if isinstance(event, LlmUsageEvent):
            self.actual_tokens += (
                event.input_tokens
                + event.output_tokens
                + event.cache_read_input_tokens
                + event.cache_creation_input_tokens
            )
            self.usage_events += 1
        await self._inner.publish(event)


class GoalBudgetProvider:
    # 绑定不可变 Goal ID，使所有主调用、压缩和子调用共享同一 token 硬停止点
    def __init__(
        self,
        inner: LLMProvider,
        service: GoalService,
        goal_id: str,
    ) -> None:
        self._inner = inner
        self._service = service
        self._goal_id = goal_id

    # 为子 Agent 的显式 route provider 复用同一 Goal 预算控制面
    def wrap(self, inner: LLMProvider) -> GoalBudgetProvider:
        return GoalBudgetProvider(inner, self._service, self._goal_id)

    # 调用前收窄输出上限，调用后按真实 usage 记账并在无法证明预算时失败关闭
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> LlmResponse:
        before = self._service.get(self._goal_id)
        if before.token_budget is None:
            return await self._inner.chat(
                messages,
                tool_schemas,
                bus,
                run_id,
                step=step,
                system=system,
                model=model,
                thinking=thinking,
            )
        estimated_input = _estimate_request_tokens(messages, tool_schemas, system)
        lease_id = f"lease-{uuid.uuid4().hex}"
        try:
            _lease_id, reserved = self._service.reserve_token_lease(
                self._goal_id,
                lease_id=lease_id,
                requested_tokens=estimated_input + _MAX_MODEL_OUTPUT_TOKENS,
                minimum_tokens=estimated_input + 1,
            )
        except ValueError as exc:
            if "temporarily reserved" in str(exc):
                raise GoalTokenBudgetReserved(
                    "goal token budget is temporarily reserved by another model call"
                ) from exc
            raise GoalTokenBudgetExhausted(
                "goal token budget cannot cover the next model request"
            ) from exc
        output_allowance = reserved - estimated_input
        if output_allowance < 1:
            self._service.settle_token_lease(
                self._goal_id,
                lease_id=lease_id,
                actual_tokens=0,
            )
            raise GoalTokenBudgetExhausted(
                "goal token budget cannot cover the next model request"
            )

        usage_bus = _GoalUsageBus(bus)
        try:
            with output_token_budget(output_allowance):
                response = await self._inner.chat(
                    messages,
                    tool_schemas,
                    usage_bus,
                    run_id,
                    step=step,
                    system=system,
                    model=model,
                    thinking=thinking,
                )
        except BaseException:
            try:
                self._service.settle_token_lease(
                    self._goal_id,
                    lease_id=lease_id,
                    actual_tokens=(
                        usage_bus.actual_tokens if usage_bus.usage_events else None
                    ),
                )
            except Exception:
                logger.exception(
                    "failed to settle goal token lease after provider failure goal_id=%s",
                    self._goal_id,
                )
            raise

        usage = response.usage
        actual_tokens: int | None
        if usage_bus.usage_events:
            actual_tokens = usage_bus.actual_tokens
        elif usage is not None:
            actual_tokens = (
                usage.input_tokens
                + usage.output_tokens
                + usage.cache_read_input_tokens
                + usage.cache_creation_input_tokens
            )
        else:
            actual_tokens = None
        self._service.settle_token_lease(
            self._goal_id,
            lease_id=lease_id,
            actual_tokens=actual_tokens,
        )
        if actual_tokens is None:
            raise GoalTokenUsageUnavailable(
                "provider omitted usage for a token-budgeted goal"
            )
        if actual_tokens > reserved:
            raise GoalTokenBudgetExhausted(
                "provider usage exceeded its reserved goal token lease"
            )
        after_event = self._service.get(self._goal_id)
        if (
            after_event.token_budget is not None
            and after_event.tokens_used >= after_event.token_budget
        ):
            raise GoalTokenBudgetExhausted("goal token budget exhausted")
        return response

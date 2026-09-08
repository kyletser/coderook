# Shared summary request seam adapted from Pi completeSummarization (MIT).
from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from code_rook.core.events.bus import EventBus
from code_rook.core.llm.attempt import AttemptBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.errors import ProviderRequestError
from code_rook.core.llm.retry import RetryPolicy
from code_rook.core.llm.types import LlmResponse

SummaryAudit = Callable[[str, dict[str, Any]], None]


class SummaryBus(EventBus):
    # 摘要正文不进入回答时间线，用量与重试状态仍交给调用方记录。
    def __init__(self, parent: EventBus) -> None:
        super().__init__()
        self._parent = parent

    # 过滤所有尝试的正文与推理，失败片段不会污染用户可见回答。
    async def publish(self, event: BaseModel) -> None:
        if getattr(event, "type", "") == "llm.usage":
            event = event.model_copy(update={"purpose": "summary"})
        if getattr(event, "type", "") not in {"llm.token", "llm.reasoning", "agent.message"}:
            await self._parent.publish(event)


# 同一步有界重发摘要请求，认证错误、截断与用户取消不自动绕过。
async def complete_summary(
    provider: LLMProvider, *, messages: list[dict[str, object]], bus: EventBus,
    run_id: str, system: str, step: int = 0, retry_policy: RetryPolicy | None = None,
    audit: SummaryAudit | None = None,
) -> LlmResponse:
    from code_rook.core.bus.events import LlmRetryEvent
    from code_rook.core.turn.watchdog import NoContentResponseError

    policy = retry_policy or RetryPolicy()
    summary_bus = SummaryBus(bus)
    request_id = f"summary-{uuid.uuid4().hex}"
    frozen_messages = deepcopy(messages)
    request = {"messages": frozen_messages, "system": system, "tool_schemas": []}
    digest = hashlib.sha256(json.dumps(
        request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    # 以独立摘要请求 ID 记录请求和尝试，不覆盖主任务的请求快照
    def record(event_type: str, payload: dict[str, Any]) -> None:
        if audit is not None:
            audit(event_type, {
                "request_id": request_id, "request_digest": digest,
                "run_id": run_id, "step": step, **payload,
            })

    record("llm.summary_request", {"request": deepcopy(request),
                                   "provider": type(provider).__name__})
    attempts = 0
    while True:
        attempt_bus = AttemptBus(summary_bus)
        try:
            response = await provider.chat(
                messages=deepcopy(frozen_messages), tool_schemas=[], bus=attempt_bus,
                run_id=run_id, system=system, step=step,
            )
            if (
                not response.text.strip() and not response.tool_calls
                and response.completion_status in {None, "completed"}
                and response.stop_reason in {"end_turn", "stop", "completed"}
            ):
                raise NoContentResponseError("summary response is empty")
        except BaseException as exc:
            record("llm.summary_attempt", {
                "attempt": attempts + 1,
                "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                "failure_code": getattr(exc, "failure_code", type(exc).__name__),
                "fragments": attempt_bus.fragments, "truncated": attempt_bus.truncated,
            })
            if not isinstance(exc, (
                ProviderRequestError, NoContentResponseError, TimeoutError, ConnectionError,
            )):
                raise
            if isinstance(exc, ProviderRequestError) and not exc.retryable:
                raise
            delay = policy.delay(
                attempts + 1, exc.retry_after_s if isinstance(exc, ProviderRequestError) else None,
            )
            if attempts >= policy.max_retries or delay is None:
                raise
            attempts += 1
            empty = isinstance(exc, NoContentResponseError)
            code = exc.failure_code if isinstance(exc, ProviderRequestError) else (
                "empty_response" if empty else "transport_error"
            )
            await summary_bus.publish(LlmRetryEvent(
                run_id=run_id, step=step, kind="no_content" if empty else "transient",
                attempt=attempts, reason="Retrying summary request", failure_code=code,
                delay_ms=round(delay * 1000), max_retries=policy.max_retries,
                ts=datetime.now(UTC).isoformat(),
            ))
            await asyncio.sleep(delay)
        else:
            record("llm.summary_attempt", {
                "attempt": attempts + 1, "status": "received",
                "response": asdict(response), "fragments": attempt_bus.fragments,
                "truncated": attempt_bus.truncated,
            })
            return response

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel

from code_rook.core.agent_runtime.loop import EventSink, LoopPorts, Message, run_agent_loop
from code_rook.core.agent_runtime.messages import from_provider, to_provider
from code_rook.core.bus.events import (
    AgentMessageEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFailedEvent,
    ToolCallStartedEvent,
)
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.execution.invariants import InvariantViolation
from code_rook.core.goal.budget import GoalBudgetError
from code_rook.core.llm.errors import ProviderRequestError
from code_rook.core.llm.types import (
    LlmResponse,
    ToolCallBlock,
    completion_status_from_reason,
    estimate_request_input_tokens,
)
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.invocation import PreparedToolArguments, prepare_tool_arguments
from code_rook.core.tools.spec import ResourceClaim
from code_rook.core.turn import NoContentResponseError
from code_rook.core.turn.watchdog import StreamWatchdogError

if TYPE_CHECKING:
    from code_rook.core.loop import AgentLoop


_STREAM_UPDATE_MIN_CHARS = 48
_STREAM_UPDATE_MAX_INTERVAL_S = 0.08


# 判断累计正文是否已达到一次可见刷新，避免每个 token 都持久化完整消息快照。
def _should_emit_stream_update(
    *,
    text: str,
    thinking: str,
    previous_chars: int,
    previous_at: float,
    now: float,
) -> bool:
    current_chars = len(text) + len(thinking)
    return (
        current_chars - previous_chars >= _STREAM_UPDATE_MIN_CHARS
        or now - previous_at >= _STREAM_UPDATE_MAX_INTERVAL_S
    )


# 以 Python 双层循环驱动现有 Provider、权限和持久化能力，不运行外部代理进程。
async def execute_context(runtime: AgentLoop, context: ExecutionContext) -> None:
    from code_rook.core.loop import RouteCapabilityError

    bus = runtime._bus
    call_index: dict[str, int] = {}
    calls: list[ToolCallBlock] = []
    prepared_calls: dict[str, PreparedToolArguments] = {}
    results: dict[str, ToolResult] = {}
    recorded: set[str] = set()
    last_response: Any = None
    failure_reason = "runtime_error"
    usage_message_count = 0
    route_identity = {
        key: str(runtime._request_metadata.get(key, ""))
        for key in ("wire_format", "model", "route_id")
    }
    restored_response = None
    restore_usage = getattr(runtime._transcript, "restore_context_usage", None)
    if callable(restore_usage):
        anchor = restore_usage(to_provider(from_provider(context.messages)), route_identity)
        if anchor is not None:
            usage, usage_message_count = anchor
            restored_response = LlmResponse(stop_reason="end_turn", usage=usage)
    compacted_history: tuple[int, list[Message]] | None = None
    follow_up_admissions: deque[Callable[[], Awaitable[None]]] = deque()
    lifecycle_started = False
    lifecycle_ended = False
    lifecycle_messages: list[Message] = []
    permission_denied_mutation = False
    successful_mutation = False

    # 将消息生命周期直接传给双前端，正文和思考使用不同内容块。
    async def emit(event: dict[str, Any]) -> None:
        nonlocal lifecycle_started, lifecycle_ended
        kind = event["type"]
        timestamp = datetime.now(UTC).isoformat()
        if kind == "agent_start":
            lifecycle_started = True
        elif kind == "agent_end":
            lifecycle_ended = True
        if runtime._agent_event_sink is not None:
            emitted = await runtime._agent_event_sink(event)
            if kind == "message_end" and emitted is not None:
                replacement = emitted.get("message")
                target = event.get("message")
                if isinstance(target, dict) and isinstance(replacement, dict):
                    target.clear()
                    target.update(deepcopy(replacement))
        if kind == "message_end":
            lifecycle_messages.append(deepcopy(event["message"]))
        if kind == "turn_start":
            context.step += 1
            runtime._active_step = context.step
            await bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=timestamp)
            )
        elif kind == "turn_end":
            runtime._active_step = 0
            await bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=timestamp)
            )
        elif kind == "tool_execution_end" and event["toolCallId"] not in results:
            call = calls[call_index[event["toolCallId"]]]
            await bus.publish(
                ToolCallStartedEvent(
                    run_id=context.run_id,
                    tool_use_id=call.id,
                    tool_name=call.name,
                    params=call.input,
                    step=context.step,
                    ts=timestamp,
                )
            )
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=context.run_id,
                    tool_use_id=call.id,
                    tool_name=call.name,
                    error_class="invalid_arguments",
                    error_message="Tool call was not executed",
                    elapsed_ms=0,
                    step=context.step,
                    ts=timestamp,
                )
            )
        elif kind in {"message_start", "message_update", "message_end"}:
            message = event["message"]
            if message["role"] == "assistant":
                await bus.publish(
                    AgentMessageEvent(
                        run_id=context.run_id,
                        message_id=f"{context.run_id}:{context.step}",
                        phase=cast(
                            Literal["start", "update", "end"],
                            {
                                "message_start": "start",
                                "message_update": "update",
                                "message_end": "end",
                            }[kind],
                        ),
                        role="assistant",
                        content=deepcopy(message.get("content", [])),
                        stop_reason=message.get("stopReason"),
                        backend="python",
                        step=context.step,
                        ts=timestamp,
                    )
                )
                if kind == "message_end" and runtime._transcript:
                    runtime._transcript.append_assistant(
                        context.step, to_provider([message])[0]["content"]
                    )
            elif kind == "message_end" and message["role"] == "toolResult":
                call_id = message["toolCallId"]
                result = results.pop(call_id, None) or ToolResult(
                    content="\n".join(block.get("text", "") for block in message["content"]),
                    is_error=bool(message.get("isError")),
                )
                await runtime._record_result(
                    call_index[call_id], len(calls), calls[call_index[call_id]], result, context
                )
                recorded.add(call_id)
            elif kind == "message_end" and message["role"] == "user":
                runtime._stuck_guard.reset()
                if runtime._transcript:
                    runtime._transcript.append_user(context.step, deepcopy(message["content"]))
                # 后续输入按领取顺序发布；确认回调属于驱动状态，不进入消息历史。
                if follow_up_admissions:
                    await follow_up_admissions.popleft()()
            elif kind == "message_end" and message["role"] == "custom":
                runtime._stuck_guard.reset()
                ledger_seq = None
                if runtime._transcript:
                    ledger_seq = runtime._transcript.append_custom(
                        step=context.step,
                        custom_type=str(message.get("customType", "message")),
                        content=deepcopy(message.get("content", [])),
                        display=bool(message.get("display", False)),
                        details=deepcopy(message.get("details")),
                    )
                if message.get("display", False):
                    content = message.get("content", [])
                    await bus.publish(AgentMessageEvent(
                        run_id=context.run_id,
                        message_id=f"{context.run_id}:extension:{context.step}",
                        phase="end",
                        role="custom",
                        custom_type=str(message.get("customType", "message")),
                        content=content if isinstance(content, list) else [
                            {"type": "text", "text": str(content)},
                        ],
                        ledger_seq=ledger_seq,
                        step=context.step,
                        ts=timestamp,
                    ))
                if follow_up_admissions:
                    await follow_up_admissions.popleft()()

    # 将 Provider 的增量内容累积为同一 assistant 消息，而非依赖交错事件推测正文。
    async def stream(messages: list[Message], sink: EventSink) -> Message:
        nonlocal last_response, calls, call_index, compacted_history, usage_message_count
        nonlocal failure_reason
        context.messages = to_provider(messages)
        partial: Message = {"role": "assistant", "content": [], "stopReason": None}
        text = ""
        thinking = ""
        emitted_chars = 0
        emitted_at = time.monotonic()
        await sink({"type": "message_start", "message": partial})

        # 捕获文本事件并继续转发用量、重试与审计事件。
        async def forward(event: BaseModel) -> None:
            nonlocal text, thinking, emitted_chars, emitted_at
            payload = event.model_dump()
            if payload.get("type") == "llm.token":
                text += str(payload["token"])
            elif payload.get("type") == "llm.reasoning":
                thinking += str(payload["content"])
            else:
                await bus.publish(event)
                return
            partial["content"] = ([{"type": "text", "text": text}] if text else []) + (
                [{"type": "thinking", "thinking": thinking}] if thinking else []
            )
            now = time.monotonic()
            if not _should_emit_stream_update(
                text=text,
                thinking=thinking,
                previous_chars=emitted_chars,
                previous_at=emitted_at,
                now=now,
            ):
                return
            await sink({"type": "message_update", "message": partial})
            emitted_chars = len(text) + len(thinking)
            emitted_at = now

        provider_bus = EventBus()
        provider_bus.subscribe(forward, critical=True)
        runtime._bus = provider_bus
        failure_reason = "llm_error"
        try:
            try:
                last_response = await runtime._call_provider(context)
            except Exception as exc:
                if not (
                    runtime._compactor
                    and runtime._is_context_error(exc)
                    and not runtime._reactive_compaction_attempted
                ):
                    raise
                runtime._reactive_compaction_attempted = True
                failure_reason = "runtime_error"
                compacted = await runtime._compactor.compact(
                    context,
                    runtime._provider,
                    trigger="overflow",
                    focus="Preserve the current goal and complete recent tool-use pairs.",
                )
                if compacted is None:
                    raise
                compacted_history = (len(messages), from_provider(context.messages))
                text = thinking = ""
                partial["content"] = []
                await sink({"type": "message_update", "message": partial})
                emitted_chars = 0
                emitted_at = time.monotonic()
                failure_reason = "llm_error"
                last_response = await runtime._call_provider(context)
        finally:
            runtime._bus = bus
        failure_reason = "runtime_error"
        status = last_response.completion_status or completion_status_from_reason(
            last_response.stop_reason, has_tool_calls=bool(last_response.tool_calls)
        )
        calls = last_response.tool_calls
        prepared_calls.clear()
        prepared_calls.update(
            (call.id, prepare_tool_arguments(runtime._registry, call)) for call in calls
        )
        recorded.clear()
        ports.sequential_tools = frozenset(
            call.name for call in calls
            if prepared_calls[call.id].error is not None
            or runtime._parallel_claims(prepared_calls[call.id].call) is None
        )
        batch_claims: list[ResourceClaim] = []
        for call in calls:
            prepared = prepared_calls[call.id]
            claims = None if prepared.error is not None else runtime._parallel_claims(prepared.call)
            if claims is not None:
                if not runtime._can_join_parallel_batch(claims, batch_claims):
                    ports.sequential_tools = frozenset(item.name for item in calls)
                    break
                batch_claims.extend(claims)
        call_index = {call.id: index for index, call in enumerate(calls)}
        blocks = deepcopy(last_response.thinking_blocks)
        for block in blocks:
            block["_coderook_source"] = {
                key: str(runtime._request_metadata.get(key, ""))
                for key in ("wire_format", "model", "route_id")
            }
        if last_response.text:
            blocks.append({"type": "text", "text": last_response.text})
        blocks += [
            {"type": "toolCall", "id": call.id, "name": call.name, "arguments": call.input}
            for call in calls
        ]
        context_window = getattr(runtime._provider, "context_window", None)
        message = {
            "role": "assistant",
            "content": blocks,
            "stopReason": {"completed": "stop", "tool_use": "toolUse", "length": "length"}.get(
                status, "error"
            ),
            "usage": asdict(last_response.usage) if last_response.usage is not None else None,
            "contextWindow": context_window if type(context_window) is int else None,
        }
        context.add_assistant_message(to_provider([message])[0]["content"])
        usage_message_count = len(context.messages)
        if message["stopReason"] == "error":
            context.result = last_response.text
            context.mark_failed(status)
        await sink({"type": "message_end", "message": message})
        append_usage = getattr(runtime._transcript, "append_context_usage", None)
        if last_response.usage is not None and callable(append_usage):
            append_usage(context.step, context.messages, last_response.usage, route_identity)
        return message

    # 工具实现和审批仍使用 Python 的单一调用管线，结果由新循环按顺序入账。
    async def execute(call: dict[str, Any], sink: EventSink) -> dict[str, Any]:
        nonlocal permission_denied_mutation, successful_mutation
        tool_call = calls[call_index[call["id"]]]
        result = await runtime._invoke_one(
            tool_call, context,
            prepared_arguments=prepared_calls[call["id"]],
        )
        if runtime._is_mutating_call(tool_call):
            if result.error_type == "permission_denied":
                permission_denied_mutation = True
            elif not result.is_error:
                successful_mutation = True
        results[call["id"]] = result
        content = result.model_content()
        return {"content": [{"type": "text", "text": content}]
                if isinstance(content, str) else content, "isError": result.is_error,
                "terminate": result.terminate, "details": result.details}

    # 用户纠偏只进入下一次模型请求，不在任务开始前做关键词分类。
    async def steering() -> list[Message]:
        manager = runtime._interaction_manager
        if manager is None:
            return []
        return [
            deepcopy(content)
            if isinstance(content, dict) and content.get("role") == "custom"
            else {"role": "user", "content": content}
            for content in manager.drain_steering(context.run_id)
        ]

    # 主任务不再调用工具时才领取后续输入，保持同一循环和模型上下文。
    async def follow_up() -> list[Message]:
        manager = runtime._interaction_manager
        if manager is None:
            return []
        messages = await manager.drain_follow_up(context.run_id)
        follow_up_admissions.extend(message.admitted for message in messages)
        return [
            deepcopy(message.content)
            if isinstance(message.content, dict) and message.content.get("role") == "custom"
            else {"role": "user", "content": deepcopy(message.content)}
            for message in messages
        ]

    # 每轮结束后使用原有显式预算上限，不根据 TODO 状态阻止最终回答。
    async def should_stop(message: Message, tool_results: list[Message]) -> bool:
        if context.is_done():
            return True
        if (
            tool_results and not all(result.get("terminate") for result in tool_results)
            and context.max_steps > 0 and context.step >= context.max_steps
        ):
            if not await runtime._try_continue_past_max_steps(context):
                context.mark_failed("exceeded_max_steps")
                return True
        return False

    # 压缩发生于完整工具结果之后，给下一步返回新的上下文投影。
    async def prepare(messages: list[Message]) -> list[Message]:
        nonlocal compacted_history
        if compacted_history is not None:
            previous_count, replacement = compacted_history
            messages = replacement + messages[previous_count:]
            compacted_history = None
        context.messages = to_provider(messages)
        await runtime._flush_repeat_notices(context)
        await runtime._apply_tool_result_budget(context)
        initial_pressure = False
        usage_response = last_response or restored_response
        window = getattr(runtime._provider, "context_window", None)
        if (
            (last_response is None or last_response.usage is None)
            and isinstance(window, int) and window > 0 and runtime._compactor
        ):
            input_tokens = estimate_request_input_tokens(
                context.messages,
                runtime._registry.tool_schemas() if runtime._supports_tools else [],
                runtime._render_system(context),
            )
            initial_pressure = (
                input_tokens / window >= runtime._compact_threshold
                or input_tokens + runtime._compactor.reserve_tokens >= window
            )
        if (
            runtime._compactor
            and runtime._compact_threshold > 0
            and (
                initial_pressure
                or usage_response and usage_response.usage and (
                    usage_response.usage.context_pct >= runtime._compact_threshold
                    or runtime._projected_context_pct(
                        context, usage_response,
                        output_reserve_tokens=runtime._compactor.reserve_tokens,
                        usage_message_count=usage_message_count,
                    ) >= 1.0
                )
            )
        ):
            await runtime._compactor.compact(context, runtime._provider, trigger="auto_threshold")
        return from_provider(context.messages)

    ports = LoopPorts(
        stream,
        execute,
        emit,
        steering=steering,
        follow_up=follow_up,
        prepare_request=prepare,
        should_stop=should_stop,
        parallel=runtime._supports_parallel_tools,
    )
    try:
        added = await run_agent_loop(
            from_provider(context.messages),
            [],
            ports,
        )
        if not context.is_done():
            final = next((item for item in reversed(added) if item["role"] == "assistant"), {})
            context.result = "\n".join(
                block.get("text", "")
                for block in final.get("content", [])
                if block.get("type") == "text"
            )
            task_profile = runtime._request_metadata.get("task_profile", {})
            mutating_task = (
                isinstance(task_profile, dict)
                and task_profile.get("risk") in {"mutate", "write"}
            )
            if mutating_task and permission_denied_mutation and not successful_mutation:
                context.mark_failed("permission_denied")
            elif final.get("stopReason") == "stop" and context.result.strip():
                context.mark_success()
            else:
                context.mark_failed("incomplete")
    except asyncio.CancelledError:
        context.mark_failed("cancelled")
        for index, call in enumerate(calls):
            if call.id not in recorded:
                result = results.pop(call.id, None) or ToolResult(
                    content="Skipped: run cancelled; execution may have been interrupted.",
                    is_error=True,
                )
                await runtime._record_result(index, len(calls), call, result, context)
        raise
    except (StreamWatchdogError, NoContentResponseError, GoalBudgetError) as exc:
        context.mark_failed(exc.reason)
    except RouteCapabilityError:
        context.mark_failed("route_capability_error")
    except InvariantViolation:
        context.mark_failed("invariant_violation")
    except ProviderRequestError as exc:
        context.result = str(exc)
        context.mark_failed(
            "transport_error" if exc.failure_code in {"timeout", "transport_error"}
            else exc.failure_code
        )
    except Exception:
        logging.getLogger(__name__).exception("Python agent run failed: %s", context.run_id)
        context.mark_failed(failure_reason)
    finally:
        runtime._bus = bus
        if runtime._agent_event_sink is not None and lifecycle_started and not lifecycle_ended:
            await runtime._agent_event_sink({
                "type": "agent_end", "messages": deepcopy(lifecycle_messages),
            })
        if runtime._agent_event_sink is not None and lifecycle_started:
            await runtime._agent_event_sink({"type": "agent_settled"})
        if runtime._hooks is not None:
            await runtime._hooks.emit(
                "turn_stop",
                {
                    "run_id": context.run_id,
                    "session_id": runtime._session_id,
                    "status": context.status,
                    "reason": context.reason,
                    "result": context.result,
                },
            )

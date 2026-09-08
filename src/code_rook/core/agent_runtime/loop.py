# Python port of Pi packages/agent/src/agent-loop.ts (MIT).
# Upstream b2602be77cb7b0de45dd616407fd210daa48aa75; license: vendor/pi/LICENSE.
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

type Message = dict[str, Any]
type EventSink = Callable[[dict[str, Any]], Awaitable[None]]
type MessageQueue = Callable[[], Awaitable[list[Message]]]
type StreamResponse = Callable[[list[Message], EventSink], Awaitable[Message]]
type ExecuteTool = Callable[[dict[str, Any], EventSink], Awaitable[dict[str, Any]]]
type PrepareTurn = Callable[[list[Message]], Awaitable[list[Message]]]
type StopAfterTurn = Callable[[Message, list[Message]], Awaitable[bool]]


@dataclass
class LoopPorts:
    stream: StreamResponse
    execute: ExecuteTool
    emit: EventSink
    steering: MessageQueue | None = None
    follow_up: MessageQueue | None = None
    prepare: PrepareTurn | None = None
    should_stop: StopAfterTurn | None = None
    parallel: bool = False
    sequential_tools: frozenset[str] = frozenset()
    prepare_request: PrepareTurn | None = None


# 发布独立消息的起止快照，调用方修改上下文不会反向篡改已发布事件。
async def _emit_message(message: Message, emit: EventSink) -> None:
    await emit({"type": "message_start", "message": deepcopy(message)})
    completed = {"type": "message_end", "message": deepcopy(message)}
    await emit(completed)
    replacement = completed.get("message")
    if isinstance(replacement, dict) and replacement.get("role") == message.get("role"):
        message.clear()
        message.update(deepcopy(replacement))


# 执行单个工具并把异常转换为模型可见结果，取消始终向外传播。
async def _execute_call(call: dict[str, Any], ports: LoopPorts, truncated: bool) -> dict[str, Any]:
    await ports.emit(
        {
            "type": "tool_execution_start",
            "toolCallId": call["id"],
            "toolName": call["name"],
            "args": deepcopy(call["arguments"]),
        }
    )
    try:
        if truncated:
            raise ValueError(
                "Output token limit reached; re-issue this tool with complete arguments."
            )
        result = await ports.execute(call, ports.emit)
    except Exception as exc:
        result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    result = {**result, "content": result.get("content") or []}
    await ports.emit(
        {
            "type": "tool_execution_end",
            "toolCallId": call["id"],
            "toolName": call["name"],
            "result": deepcopy(result),
            "isError": bool(result.get("isError")),
        }
    )
    return {"role": "toolResult", "toolCallId": call["id"], "toolName": call["name"], **result}


# 并行执行仍按模型声明顺序写入工具结果，显式顺序工具使整个批次串行化。
async def _execute_batch(
    calls: list[dict[str, Any]], ports: LoopPorts, truncated: bool
) -> list[Message]:
    if ports.parallel and not any(call["name"] in ports.sequential_tools for call in calls):
        tasks = [asyncio.create_task(
            _execute_call(call, ports, truncated), name=f"tool-call:{call['id']}",
        ) for call in calls]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    else:
        results = []
        for call in calls:
            results.append(await _execute_call(call, ports, truncated))
    for result in results:
        await _emit_message(result, ports.emit)
    return results


# 移植 Pi 双层循环：工具与纠偏驱动内层，排队消息驱动外层，不引入意图分类分支。
async def run_agent_loop(
    history: list[Message], prompts: list[Message], ports: LoopPorts, *, resume: bool = False
) -> list[Message]:
    if resume and (not history or history[-1].get("role") == "assistant"):
        raise ValueError("Continuation requires a user message or tool result")
    context = deepcopy(history + prompts)
    added = deepcopy(prompts)
    await ports.emit({"type": "agent_start"})
    await ports.emit({"type": "turn_start"})
    for prompt in prompts:
        await _emit_message(prompt, ports.emit)
    pending = await ports.steering() if ports.steering else []
    completed_turn = False
    while True:
        more_tools = True
        while more_tools or pending:
            if completed_turn:
                if ports.prepare:
                    context = await ports.prepare(context)
                if not pending and ports.steering:
                    pending = await ports.steering()
                await ports.emit({"type": "turn_start"})
            for message in pending:
                await _emit_message(message, ports.emit)
                context.append(message)
                added.append(message)
            pending = []
            if ports.prepare_request:
                context = await ports.prepare_request(context)
            # 流式适配器拥有同一条 assistant 消息的 start/update/end。
            message = await ports.stream(deepcopy(context), ports.emit)
            context.append(message)
            added.append(message)
            stop = message.get("stopReason")
            if stop in {"error", "aborted"}:
                await ports.emit({"type": "turn_end", "message": message, "toolResults": []})
                await ports.emit({"type": "agent_end", "messages": deepcopy(added)})
                return added
            calls = [
                block for block in message.get("content", []) if block.get("type") == "toolCall"
            ]
            results = await _execute_batch(calls, ports, stop == "length") if calls else []
            more_tools = bool(calls) and not all(result.get("terminate") for result in results)
            context.extend(results)
            added.extend(results)
            await ports.emit(
                {"type": "turn_end", "message": deepcopy(message), "toolResults": deepcopy(results)}
            )
            completed_turn = True
            if ports.should_stop and await ports.should_stop(message, results):
                await ports.emit({"type": "agent_end", "messages": deepcopy(added)})
                return added
            pending = await ports.steering() if ports.steering else []
        pending = await ports.follow_up() if ports.follow_up else []
        if not pending:
            break
    await ports.emit({"type": "agent_end", "messages": deepcopy(added)})
    return added

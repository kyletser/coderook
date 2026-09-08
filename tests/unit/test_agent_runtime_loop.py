import asyncio
from typing import Any

import pytest

from code_rook.core.agent_runtime.loop import LoopPorts, run_agent_loop
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager


# 功能：验证截断工具不执行，并在下一步取得真正回答。
# 设计：模型明确给出可解析但截断的参数，排除仅依赖 JSON 解析防护的实现。
async def test_truncated_tool_reissued() -> None:
    events: list[dict[str, Any]] = []
    requests: list[list[dict[str, Any]]] = []

    # 收集顺序事件用于确认工具错误回到下一次模型输入。
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    # 返回一次截断工具调用和一次完整正文。
    async def stream(messages: list[dict[str, Any]], sink: Any) -> dict[str, Any]:
        requests.append(messages)
        return {
            "role": "assistant",
            "stopReason": "length" if len(requests) == 1 else "stop",
            "content": [
                {"type": "toolCall", "id": "one", "name": "write", "arguments": {"path": "x"}}
            ]
            if len(requests) == 1
            else [{"type": "text", "text": "Finished"}],
        }

    # 截断调用不得到达真正执行函数。
    async def execute(call: dict[str, Any], sink: Any) -> dict[str, Any]:
        pytest.fail("truncated call executed")

    result = await run_agent_loop(
        [], [{"role": "user", "content": "Do task"}], LoopPorts(stream, execute, emit)
    )
    assert requests[1][-1]["isError"]
    assert result[-1]["content"][0]["text"] == "Finished"
    assert events[-1]["type"] == "agent_end"


# 功能：验证并行工具完成乱序时模型历史仍按声明顺序排列，随后消费 follow-up。
# 设计：第一个工具等待第二个完成，不使用时间碰运气；同时校验工具结果与后续输入顺序。
async def test_parallel_order_and_follow_up() -> None:
    second_done = asyncio.Event()
    requests: list[list[dict[str, Any]]] = []
    queue = [{"role": "user", "content": "Follow up"}]

    # 本用例只关注模型输入，不保留辅助事件。
    async def emit(event: dict[str, Any]) -> None:
        pass

    # 首次请求触发双工具，之后正常回答。
    async def stream(messages: list[dict[str, Any]], sink: Any) -> dict[str, Any]:
        requests.append(messages)
        return {
            "role": "assistant",
            "stopReason": "toolUse" if len(requests) == 1 else "stop",
            "content": [
                {"type": "toolCall", "id": str(i), "name": "read", "arguments": {}}
                for i in range(2)
            ]
            if len(requests) == 1
            else [{"type": "text", "text": "Answer"}],
        }

    # 制造确定性的反向完成顺序。
    async def execute(call: dict[str, Any], sink: Any) -> dict[str, Any]:
        if call["id"] == "0":
            await second_done.wait()
        else:
            second_done.set()
        return {"content": [{"type": "text", "text": call["id"]}]}

    # 仅在主任务完成时交出一条排队消息。
    async def follow_up() -> list[dict[str, Any]]:
        result = queue[:]
        queue.clear()
        return result

    await asyncio.wait_for(
        run_agent_loop(
            [],
            [{"role": "user", "content": "Task"}],
            LoopPorts(stream, execute, emit, follow_up=follow_up, parallel=True),
        ),
        2,
    )
    assert [message["toolCallId"] for message in requests[1][-2:]] == ["0", "1"]
    assert requests[2][-1]["content"] == "Follow up"


# 功能：验证连续纠偏逐轮交付，压缩期间新消息不会挤进已有纠偏的同一轮。
# 设计：使用真实交互队列并在 prepare 注入消息，确定性覆盖 Pi 的二次轮询条件。
async def test_steering_delivers_one_message_per_model_turn() -> None:
    manager = InteractionManager(EventBus())
    manager.register_run("active")
    requests: list[list[dict[str, Any]]] = []

    # 此场景只检查请求历史，不收集显示事件。
    async def emit(event: dict[str, Any]) -> None:
        pass

    # 第一轮生成正文期间用户连续纠偏两次。
    async def stream(messages: list[dict[str, Any]], sink: Any) -> dict[str, Any]:
        requests.append(messages)
        if len(requests) == 1:
            manager.steer("active", "first correction")
            manager.steer("active", "second correction")
        return {"role": "assistant", "stopReason": "stop",
                "content": [{"type": "text", "text": "Answer"}]}

    # 模拟压缩期间另一个纠偏抵达，不应在已有待发消息时额外消费。
    async def prepare(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(requests) == 1:
            manager.steer("active", "during compaction")
        return messages

    # 从真实交互管理器取出当前这一轮的纠偏。
    async def steering() -> list[dict[str, Any]]:
        return [{"role": "user", "content": text} for text in manager.drain_steering("active")]

    # 正文任务不会执行工具。
    async def execute(call: dict[str, Any], sink: Any) -> dict[str, Any]:
        pytest.fail("unexpected tool")

    await run_agent_loop([], [{"role": "user", "content": "Task"}],
                         LoopPorts(stream, execute, emit, steering=steering, prepare=prepare))
    assert len(requests) == 4
    assert [messages[-1]["content"] for messages in requests] == [
        "Task", "first correction", "second correction", "during compaction",
    ]

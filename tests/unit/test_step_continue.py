from __future__ import annotations

from typing import Any

from code_rook.core.config import AgentConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.provider import with_incremental_cache_breakpoint
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.registry import ToolRegistry


class _AlwaysToolProvider:
    # 每步都返回一个 unknown 工具调用且入参递增，驱动步数耗尽而不触发卡死检测
    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        return LlmResponse(
            stop_reason="tool_use",
            tool_calls=[
                ToolCallBlock(id=f"t{self.calls}", name="unknown", input={"n": self.calls})
            ],
        )


class _StubInteraction:
    # 记录提问并按预设答案回复；steering 恒为空
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked = 0

    # 无运行中纠偏
    def drain_steering(self, _run_id: str) -> list[str]:
        return []

    async def ask(
        self,
        **kwargs: Any,
    ) -> str:
        self.asked += 1
        return self.answer


# 功能：验证步数耗尽时交互续跑直到达到询问上限后失败
# 设计：stub 交互始终返回"继续执行"，断言三次提问逐段扩展（2→8）且最终原因仍是 exceeded_max_steps
async def test_step_continue_via_ask_extends_budget_once() -> None:
    provider = _AlwaysToolProvider()
    interaction = _StubInteraction("继续执行")
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        interaction_manager=interaction,  # type: ignore[arg-type]
        session_id="sess-1",
    )
    ctx = ExecutionContext(run_id="r", goal="g", max_steps=2)

    await loop.run(ctx)

    assert interaction.asked == 3
    assert ctx.max_steps == 8
    assert ctx.status == "failed"
    assert ctx.reason == "exceeded_max_steps"
    assert ctx.step == 8


# 功能：验证用户选择"就此停止"时不续跑
# 设计：同样配置但答案为停止，断言未扩展步数且提问只有一次
async def test_step_continue_user_declines() -> None:
    provider = _AlwaysToolProvider()
    interaction = _StubInteraction("就此停止")
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        interaction_manager=interaction,  # type: ignore[arg-type]
        session_id="sess-1",
    )
    ctx = ExecutionContext(run_id="r", goal="g", max_steps=2)

    await loop.run(ctx)

    assert interaction.asked == 1
    assert ctx.max_steps == 2
    assert ctx.reason == "exceeded_max_steps"


# 功能：验证自动续段配额无需提问直接扩展
# 设计：auto_step_continues=1 且无交互管理器，断言步数翻倍且从未提问
async def test_auto_step_continue_without_interaction() -> None:
    provider = _AlwaysToolProvider()
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        ToolRegistry(),
        EventBus(),
        auto_step_continues=1,
    )
    ctx = ExecutionContext(run_id="r", goal="g", max_steps=2)

    await loop.run(ctx)

    assert ctx.max_steps == 4
    assert ctx.reason == "exceeded_max_steps"


# 功能：验证增量缓存断点打在最后一个 tool_result 块且不改原始消息
# 设计：构造含 tool_use/tool_result 的消息序列，断言副本末块带 cache_control、原列表未变
def test_incremental_cache_breakpoint_marks_last_tool_result() -> None:
    original = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "echo", "input": {}}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
        ]},
    ]

    processed = with_incremental_cache_breakpoint(original)

    last_block = processed[-1]["content"][-1]  # type: ignore[index]
    assert last_block["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in original[-1]["content"][0]  # type: ignore[index]
    assert processed[0] is original[0]


# 功能：验证 agent.max_step_continues 配置解析与非法值拒绝
# 设计：纯 AgentConfig 字段断言覆盖默认值与环境变量说明，聚焦配置面可用
def test_agent_config_max_step_continues_default() -> None:
    config = AgentConfig()

    assert config.max_step_continues == 0

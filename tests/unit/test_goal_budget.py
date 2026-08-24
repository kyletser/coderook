from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from code_rook.core.bus.events import LlmUsageEvent
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.goal import (
    GoalBudgetProvider,
    GoalService,
    GoalStore,
    GoalTokenBudgetExhausted,
    GoalTokenBudgetReserved,
    GoalTokenUsageUnavailable,
)
from code_rook.core.llm.budget import clamp_output_token_limit
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.registry import ToolRegistry


class _UsageProvider:
    # 初始化固定 usage 的 provider，并捕获 Goal 传入的单次输出上限
    def __init__(
        self,
        usage: UsageStats | None,
        *,
        publish_usage: bool = False,
    ) -> None:
        self.usage = usage
        self.publish_usage = publish_usage
        self.calls = 0
        self.output_limit = 0

    # 返回固定响应且不发布 usage 事件，覆盖静默压缩调用的本地记账路径
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
        self.output_limit = clamp_output_token_limit(8192)
        if self.publish_usage and self.usage is not None:
            await bus.publish(
                LlmUsageEvent(
                    run_id=run_id,
                    input_tokens=self.usage.input_tokens,
                    output_tokens=self.usage.output_tokens,
                    cache_read_input_tokens=self.usage.cache_read_input_tokens,
                    cache_creation_input_tokens=self.usage.cache_creation_input_tokens,
                    context_pct=self.usage.context_pct,
                    model=model or "test-model",
                    ts="2026-01-01T00:00:00+00:00",
                )
            )
        return LlmResponse(
            stop_reason="end_turn",
            text="done",
            usage=self.usage,
            completion_status="completed",
        )


# 创建已绑定 run 的自动 Goal，供预算 provider 测试复用
def _budget_goal(
    tmp_path: Path,
    *,
    budget: int,
) -> tuple[GoalService, str]:
    service = GoalService(GoalStore(tmp_path / "goals"))
    goal = service.create(
        "bounded model usage",
        session_id="sess-budget",
        token_budget=budget,
        completion_criteria=["verified"],
    )
    service.start_run(goal.id, "run-budget")
    return service, goal.id


# 功能：验证预算 provider 在调用前收窄输出上限，并对无事件总线的真实 usage 精确记账
# 设计：使用静默 fake provider 排除 app 订阅者，核对 context-local 上限和持久 token 增量
async def test_goal_budget_provider_caps_output_and_records_silent_usage(
    tmp_path: Path,
) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=1_000)
    inner = _UsageProvider(UsageStats(input_tokens=70, output_tokens=30))
    provider = GoalBudgetProvider(inner, service, goal_id)

    response = await provider.chat(
        [{"role": "user", "content": "fix the test"}],
        [],
        EventBus(),
        "tool-distill",
        system="Be concise.",
    )

    assert response.text == "done"
    assert 0 < inner.output_limit < 8192
    assert service.get(goal_id).tokens_used == 100


# 功能：验证 provider 已发布 LlmUsageEvent 时 Goal 只记账一次而不会再按响应 usage 重复累计
# 设计：让 fake 同时发布事件并返回相同 usage，断言事件代理与响应回退路径互斥
async def test_goal_budget_provider_deduplicates_usage_event_and_response(
    tmp_path: Path,
) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=1_000)
    inner = _UsageProvider(
        UsageStats(input_tokens=70, output_tokens=30),
        publish_usage=True,
    )
    provider = GoalBudgetProvider(inner, service, goal_id)

    await provider.chat(
        [{"role": "user", "content": "one accounted request"}],
        [],
        EventBus(),
        "run-budget",
    )

    assert service.get(goal_id).tokens_used == 100


# 功能：验证实际 usage 耗尽预算后当前调用失败关闭，下一次模型请求在 provider 前被拒绝
# 设计：让 fake 首次返回恰好等于预算的 usage，再检查暂停状态和调用计数保持为一
async def test_goal_budget_provider_stops_after_actual_exhaustion(tmp_path: Path) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=1_000)
    inner = _UsageProvider(UsageStats(input_tokens=800, output_tokens=200))
    provider = GoalBudgetProvider(inner, service, goal_id)

    with pytest.raises(GoalTokenBudgetExhausted, match="exhausted"):
        await provider.chat(
            [{"role": "user", "content": "continue"}],
            [],
            EventBus(),
            "run-budget",
        )
    with pytest.raises(GoalTokenBudgetExhausted, match="cannot cover"):
        await provider.chat(
            [{"role": "user", "content": "must not run"}],
            [],
            EventBus(),
            "run-budget",
        )

    persisted = service.get(goal_id)
    assert persisted.tokens_used == 1_000
    assert persisted.status == "paused"
    assert persisted.paused_reason == "token_budget_exhausted"
    assert inner.calls == 1


# 功能：验证剩余预算连请求输入都无法覆盖时 provider 不会启动且 Goal 明确暂停
# 设计：使用极小预算触发 lease minimum 检查，核对零真实 usage、零残留预留和稳定暂停原因
async def test_goal_budget_provider_pauses_before_oversized_request(
    tmp_path: Path,
) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=10)
    inner = _UsageProvider(UsageStats(input_tokens=1, output_tokens=1))
    provider = GoalBudgetProvider(inner, service, goal_id)

    with pytest.raises(GoalTokenBudgetExhausted, match="cannot cover"):
        await provider.chat(
            [{"role": "user", "content": "too large"}],
            [],
            EventBus(),
            "run-budget",
        )

    persisted = service.get(goal_id)
    assert persisted.tokens_used == 0
    assert persisted.token_reservations == {}
    assert persisted.status == "paused"
    assert persisted.paused_reason == "token_budget_exhausted"
    assert inner.calls == 0


# 功能：验证预算 Goal 遇到 provider 缺失 usage 时保守消耗 lease 并暂停
# 设计：返回无 usage 的正常文本并断言全部预留被结算，确保未知消耗不能重新变成可用额度
async def test_goal_budget_provider_rejects_missing_usage(tmp_path: Path) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=10_000)
    provider = GoalBudgetProvider(_UsageProvider(None), service, goal_id)

    with pytest.raises(GoalTokenUsageUnavailable, match="omitted usage"):
        await provider.chat(
            [{"role": "user", "content": "unknown usage"}],
            [],
            EventBus(),
            "run-budget",
        )

    persisted = service.get(goal_id)
    assert persisted.tokens_used == 10_000
    assert persisted.token_reservations == {}
    assert persisted.status == "paused"
    assert persisted.paused_reason == "token_usage_unavailable"


class _BlockingUsageProvider(_UsageProvider):
    # 初始化可控阻塞点以并发重叠父子模型调用
    def __init__(self, usage: UsageStats) -> None:
        super().__init__(usage)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    # 在返回 usage 前阻塞，使第二调用尝试竞争同一个 Goal lease
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        self.calls += 1
        self.output_limit = clamp_output_token_limit(8192)
        self.started.set()
        await self.release.wait()
        return LlmResponse(
            stop_reason="end_turn",
            text="done",
            usage=self.usage,
            completion_status="completed",
        )


# 功能：验证父调用持有 token lease 时并发子调用无法重复预留同一余额
# 设计：阻塞父 provider 后以 wrap 模拟子 route，并断言子调用在进入 provider 前失败且结算后无残留 lease
async def test_goal_budget_provider_uses_atomic_parent_child_leases(
    tmp_path: Path,
) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=2_000)
    inner = _BlockingUsageProvider(UsageStats(input_tokens=100, output_tokens=50))
    parent = GoalBudgetProvider(inner, service, goal_id)
    child = parent.wrap(inner)
    parent_call = asyncio.create_task(
        parent.chat(
            [{"role": "user", "content": "parent"}],
            [],
            EventBus(),
            "run-parent",
        )
    )
    await inner.started.wait()

    with pytest.raises(GoalTokenBudgetReserved, match="temporarily reserved"):
        await child.chat(
            [{"role": "user", "content": "child"}],
            [],
            EventBus(),
            "run-child",
        )

    inner.release.set()
    await parent_call
    persisted = service.get(goal_id)
    assert persisted.tokens_used == 150
    assert persisted.token_reservations == {}
    assert persisted.status == "active"
    assert inner.calls == 1


# 功能：验证预算充足时父与子调用可并发持有不同 lease，并按各自真实 usage 释放余额
# 设计：用两个独立阻塞 provider 同时进入请求，检查持久 reservation 数量后再并发结算
async def test_goal_budget_provider_allows_safe_parent_child_concurrency(
    tmp_path: Path,
) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=100_000)
    parent_inner = _BlockingUsageProvider(
        UsageStats(input_tokens=100, output_tokens=50)
    )
    child_inner = _BlockingUsageProvider(
        UsageStats(input_tokens=80, output_tokens=20)
    )
    parent = GoalBudgetProvider(parent_inner, service, goal_id)
    child = parent.wrap(child_inner)
    parent_call = asyncio.create_task(
        parent.chat(
            [{"role": "user", "content": "parent"}],
            [],
            EventBus(),
            "run-parent",
        )
    )
    child_call = asyncio.create_task(
        child.chat(
            [{"role": "user", "content": "child"}],
            [],
            EventBus(),
            "run-child",
        )
    )
    await asyncio.gather(parent_inner.started.wait(), child_inner.started.wait())
    in_flight = service.get(goal_id)
    assert len(in_flight.token_reservations) == 2
    assert sum(in_flight.token_reservations.values()) < 100_000

    parent_inner.release.set()
    child_inner.release.set()
    await asyncio.gather(parent_call, child_call)
    persisted = service.get(goal_id)
    assert persisted.tokens_used == 250
    assert persisted.token_reservations == {}


# 功能：验证模型调用取消且没有 usage 时会保守结算全部 lease，不留下可重复消费额度
# 设计：在 provider 阻塞期间取消任务，核对 CancelledError 保留且 Goal 预算进入暂停终态
async def test_goal_budget_provider_settles_cancelled_unknown_usage(
    tmp_path: Path,
) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=1_500)
    inner = _BlockingUsageProvider(UsageStats(input_tokens=100, output_tokens=50))
    provider = GoalBudgetProvider(inner, service, goal_id)
    call = asyncio.create_task(
        provider.chat(
            [{"role": "user", "content": "cancel me"}],
            [],
            EventBus(),
            "run-cancelled",
        )
    )
    await inner.started.wait()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    persisted = service.get(goal_id)
    assert persisted.tokens_used == 1_500
    assert persisted.token_reservations == {}
    assert persisted.status == "paused"
    assert persisted.paused_reason == "token_usage_unavailable"


# 功能：验证 AgentLoop 将预算终止报告为 token_budget_exhausted 而不是笼统 llm_error
# 设计：用单步 Loop 消费完整预算并检查 ExecutionContext 的稳定失败原因
async def test_agent_loop_preserves_goal_budget_failure_reason(tmp_path: Path) -> None:
    service, goal_id = _budget_goal(tmp_path, budget=1_000)
    provider = GoalBudgetProvider(
        _UsageProvider(UsageStats(input_tokens=900, output_tokens=100)),
        service,
        goal_id,
    )
    context = ExecutionContext(
        run_id="run-budget",
        goal="bounded",
        max_steps=1,
    )
    loop = AgentLoop(provider, ToolRegistry(), EventBus())

    await loop.run(context)

    assert context.status == "failed"
    assert context.reason == "token_budget_exhausted"

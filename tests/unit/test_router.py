from __future__ import annotations

import pytest

from code_rook.core.authority import RuntimeMode
from code_rook.core.llm.router import (
    RoutingPolicy,
    is_brief_question,
    select_route_id,
)


# 功能：static 策略在任何模式下都返回 None，即始终沿用活动路由
# 设计：遍历 ACT/PLAN 组合验证策略开关关闭时决策不会主动切换路由
def test_static_policy_always_returns_none() -> None:
    policy = RoutingPolicy(strategy="static", plan_route_id="plan", act_route_id="act")
    assert select_route_id(policy, mode=RuntimeMode.ACT, step=1) is None
    assert select_route_id(policy, mode=RuntimeMode.PLAN, step=5) is None


# 功能：rule_based 在 PLAN 模式命中高推理路由，ACT 模式回落到普通路由
# 设计：分别断言两种模式走各自配置，验证 mode 是路由选择的关键维度
def test_rule_based_selects_by_runtime_mode() -> None:
    policy = RoutingPolicy(strategy="rule_based", plan_route_id="plan", act_route_id="act")
    assert select_route_id(policy, mode=RuntimeMode.PLAN, step=1) == "plan"
    assert select_route_id(policy, mode=RuntimeMode.ACT, step=1) == "act"


# 功能：rule_based 未配置 ACT 路由时才回落到活动路由（None）
# 设计：仅设 plan_route_id，验证缺省时不会因模式无配置而报错
def test_rule_based_falls_back_when_act_unset() -> None:
    policy = RoutingPolicy(strategy="rule_based", plan_route_id="plan")
    assert select_route_id(policy, mode=RuntimeMode.ACT, step=1) is None
    assert select_route_id(policy, mode=RuntimeMode.PLAN, step=1) == "plan"


# 功能：cost_budget 在累计成本超限时降档到廉价路由，未超限则沿用活动路由
# 设计：用阈值内外两个成本值验证预算边界判断，并把未配置降档路由作为 None 兜底
def test_cost_budget_downgrades_when_over_budget() -> None:
    policy = RoutingPolicy(
        strategy="cost_budget",
        cost_budget_usd=1.0,
        cost_fallback_route_id="cheap",
    )
    assert select_route_id(policy, mode=RuntimeMode.ACT, step=1, cost_usd=0.5) is None
    assert select_route_id(policy, mode=RuntimeMode.ACT, step=1, cost_usd=1.5) == "cheap"


# 功能：cost_budget 未设降档路由时超限仅回落到活动路由
# 设计：cost_fallback 为空时返回 None，验证降档目标缺失时的安全兜底
def test_cost_budget_without_fallback_keeps_active() -> None:
    policy = RoutingPolicy(strategy="cost_budget", cost_budget_usd=1.0)
    assert select_route_id(policy, mode=RuntimeMode.ACT, step=1, cost_usd=2.0) is None


# 功能：多次连续调用使用事件计成本的上下文，最终同一次回调读取到的是累计成本
# 设计：从配置字符串构造策略时未知值回退到 static，验证 from_config 的容错
def test_from_config_falls_back_to_static_on_unknown_strategy() -> None:
    policy = RoutingPolicy.from_config("mystery")
    assert policy.strategy == "static"
    assert select_route_id(policy, mode=RuntimeMode.PLAN, step=1) is None


# 功能：only 一次简短且无工具调用的问题被识别为轻量问答
# 设计：用 step/has_tools/text_len 三个边界条件矩阵验证 is_brief_question 的判定
@pytest.mark.parametrize(
    ("step", "has_tools", "length", "expected"),
    [
        (1, False, 100, True),
        (2, False, 100, False),
        (1, True, 100, False),
        (1, False, 500, False),
    ],
)
def test_is_brief_question(step: int, has_tools: bool, length: int, expected: bool) -> None:
    assert is_brief_question(step, has_tools, length) is expected
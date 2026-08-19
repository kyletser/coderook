from __future__ import annotations

from dataclasses import dataclass

from code_rook.core.authority import RuntimeMode

# 支持的路由策略：static 固定活动路由；rule_based 按模式选路由；cost_budget 按预算降档
ROUTER_STRATEGIES = ("static", "rule_based", "cost_budget")


@dataclass(frozen=True)
class RoutingPolicy:
    # 路由策略：static | rule_based | cost_budget
    strategy: str = "static"
    # rule_based：PLAN 模式用高推理路由、其他模式用默认路由、纯问答用轻量路由
    plan_route_id: str = ""
    act_route_id: str = ""

    # cost_budget：单 run 累计成本超限后降档到廉价路由（USD）
    cost_budget_usd: float = 0.0
    cost_fallback_route_id: str = ""

    @classmethod
    def from_config(
        cls, router: str, *, plan_route_id: str = "", act_route_id: str = ""
    ) -> RoutingPolicy:
        # 从不含子配置的旧字符串拓扑构建策略，保证 backward-compatible
        return cls(
            strategy=router if router in ROUTER_STRATEGIES else "static",
            plan_route_id=plan_route_id,
            act_route_id=act_route_id,
        )


# 按策略决定应使用哪个路由；返回 None 表示沿用活动路由（static 或规则未命中）
def select_route_id(
    policy: RoutingPolicy,
    *,
    mode: RuntimeMode,
    step: int,
    cost_usd: float = 0.0,
) -> str | None:
    if policy.strategy == "static":
        return None
    if policy.strategy == "rule_based":
        if policy.plan_route_id and mode == RuntimeMode.PLAN:
            return policy.plan_route_id
        if policy.act_route_id:
            return policy.act_route_id
        return None
    if policy.strategy == "cost_budget":
        if policy.cost_budget_usd > 0 and cost_usd > policy.cost_budget_usd:
            return policy.cost_fallback_route_id or None
        return None
    return None

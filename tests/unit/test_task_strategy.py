from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.strategy import (
    TaskIntent,
    TaskRisk,
    TaskStrategy,
    TaskStrategyRouter,
)
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ToolActionSpec,
    ToolCapability,
    ToolCatalogError,
    ToolSpec,
)


class _FileFamily(BaseTool):
    name = "File"
    description = "test family"

    # 返回同时含只读和写入动作的最小 family schema
    def build_spec(self) -> ToolSpec:
        actions = tuple(
            ToolActionSpec(
                name=name,
                description=name,
                input_schema={"type": "object", "properties": {}},
                capabilities=frozenset(
                    {
                        ToolCapability.READ
                        if name == "read"
                        else ToolCapability.WRITE
                    }
                ),
            )
            for name in ("read", "write")
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema={"type": "object"},
            actions=actions,
            capabilities=frozenset({ToolCapability.READ, ToolCapability.WRITE}),
        )

    # 返回固定成功结果供目录解析测试使用
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(str(params))


# 功能：验证明确 Shell 风险由规则直接识别且混合路由不会浪费分类调用
# 设计：传入可计数 provider stub，同时断言风险结果和零次模型调用
async def test_hybrid_router_skips_model_for_clear_shell_risk() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock()

    profile = await TaskStrategyRouter().classify(
        "运行 pytest 命令并修复失败测试",
        provider=provider,
    )

    assert profile.risk == TaskRisk.SHELL
    assert profile.confidence >= 0.75
    provider.chat.assert_not_called()


# 功能：验证普通产品问答会直接响应且不会暴露任何代码执行工具
# 设计：覆盖模型身份和能力询问的真实表达，避免措辞变化再次误入规划流程
@pytest.mark.parametrize(
    "goal",
    ["你是什么模型", "具体型号呢", "你能干什么", "你有什么功能"],
)
def test_conversation_question_routes_to_direct_answer_without_tools(goal: str) -> None:
    profile = TaskStrategyRouter().classify_rules(goal)

    assert profile.intent == TaskIntent.ANSWER
    assert profile.strategy == TaskStrategy.DIRECT
    assert profile.model_tool_allowlist() == frozenset()
    assert profile.confidence >= 0.95


# 功能：验证编码任务画像会按具体意图和用户目标生成不同的执行说明
# 设计：对比修复与解释任务的标题素材，防止所有任务退化成同一段固定模板
def test_task_profile_summary_mentions_current_goal_and_intent() -> None:
    router = TaskStrategyRouter()

    fix_profile = router.classify_rules("修复登录失败并补充测试")
    explain_profile = router.classify_rules("解释认证流程")

    assert "修复登录失败并补充测试" in fix_profile.user_summary
    assert "解释认证流程" in explain_profile.user_summary
    assert fix_profile.user_summary != explain_profile.user_summary


# 功能：验证歧义任务只调用一次结构化分类并产生带摘要的冻结画像
# 设计：让 stub 返回合法 JSON，检查调用次数、来源和 digest，隔离真实模型波动
async def test_hybrid_router_classifies_ambiguous_task_once() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            text=json.dumps(
                {
                    "intent": "refactor",
                    "scope": "multi_file",
                    "risk": "mutate",
                    "strategy": "plan_first",
                    "context_policy": "long_task",
                    "confidence": 0.9,
                    "signals": ["semantic_refactor"],
                    "delegation_allowed": False,
                }
            ),
            usage=UsageStats(input_tokens=30, output_tokens=20),
        )
    )

    profile = await TaskStrategyRouter().classify("把这里处理得更合理", provider=provider)

    assert profile.source == "hybrid"
    assert profile.strategy == TaskStrategy.PLAN_FIRST
    assert len(profile.digest) == 64
    provider.chat.assert_awaited_once()


# 功能：验证模型无效或低置信度时固定回退先规划且关闭多 Agent
# 设计：用非法 JSON 触发失败关闭路径，断言委派权限不会因分类失败扩大
async def test_hybrid_router_invalid_json_fails_closed() -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="not-json"))

    profile = await TaskStrategyRouter().classify("帮我处理一下", provider=provider)

    assert profile.strategy == TaskStrategy.PLAN_FIRST
    assert not profile.delegation_allowed
    assert "safe_fallback" in profile.signals


# 功能：验证单 Agent 对照策略会隐藏委派而不降低已识别风险
# 设计：对明确跨文件修改应用实验策略，比较风险与委派字段的单向变化
async def test_single_policy_disables_delegation_without_lowering_risk() -> None:
    profile = await TaskStrategyRouter().classify(
        "修改整个仓库的多个模块并运行测试",
        method="rules_only",
        delegation_policy="single",
    )

    assert not profile.delegation_allowed
    assert profile.strategy == TaskStrategy.PLAN_FIRST
    assert profile.risk == TaskRisk.SHELL


# 功能：验证只读 TaskProfile 同时从 schema 和执行解析隐藏 File.write
# 设计：注册含 read/write 的最小 family，检查模型目录后再直接尝试越权调用
def test_read_profile_filters_family_actions_fail_closed() -> None:
    profile = TaskStrategyRouter().classify_rules("解释这个文件")
    registry = ToolRegistry()
    registry.register(_FileFamily())
    registry.set_model_tool_allowlist(profile.model_tool_allowlist())
    registry.set_model_action_allowlist(profile.model_action_allowlist())

    schema = registry.tool_schemas()[0]["input_schema"]
    assert isinstance(schema, dict)
    assert len(schema["oneOf"]) == 1
    registry.resolve_call("File", {"action": "read"})
    with pytest.raises(ToolCatalogError, match="hidden"):
        registry.resolve_call("File", {"action": "write"})


# 功能：验证可写任务在 Plan Ticket 签发前仍只暴露 File/Git 的只读 action
# 设计：使用明确跨文件修改画像应用 planning allowlist，直接尝试 File.write 证明 Core 门禁独立于风险字段
def test_plan_gate_filters_mutating_family_actions() -> None:
    profile = TaskStrategyRouter().classify_rules("修改整个仓库的多个模块")
    registry = ToolRegistry()
    registry.register(_FileFamily())
    registry.set_model_tool_allowlist(profile.planning_tool_allowlist())
    registry.set_model_action_allowlist(profile.planning_action_allowlist())

    registry.resolve_call("File", {"action": "read"})
    with pytest.raises(ToolCatalogError, match="hidden"):
        registry.resolve_call("File", {"action": "write"})

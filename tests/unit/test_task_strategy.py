from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.strategy import (
    TaskIntent,
    TaskRisk,
    TaskScope,
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


# 功能：验证目录内容询问被识别为直接只读检查并开放目录读取工具
# 设计：覆盖用户真实中文表达和常见同义句，防止无修改意图的简单查询误入 Plan 门禁
@pytest.mark.parametrize(
    "goal",
    ["当前文件夹有什么。", "列出当前目录", "这个目录下有哪些文件"],
)
def test_directory_listing_routes_to_direct_read(goal: str) -> None:
    profile = TaskStrategyRouter().classify_rules(goal)

    assert profile.intent == TaskIntent.INSPECT
    assert profile.risk == TaskRisk.READ
    assert profile.strategy == TaskStrategy.DIRECT
    assert "list_dir" in (profile.model_tool_allowlist() or frozenset())
    assert profile.confidence >= 0.9


# 功能：验证否定动作、名词化“实现”和路径中的 web 不会伪造修改或外部访问风险
# 设计：覆盖真实误判措辞并比较意图、范围和风险，防止子串规则再次扩大执行权限
@pytest.mark.parametrize(
    ("goal", "intent", "risk"),
    [
        ("解释 src/auth.py 里的登录流程，不要修改代码。", TaskIntent.EXPLAIN, TaskRisk.READ),
        ("定位 Provider 路由选择的实现和调用方。", TaskIntent.INSPECT, TaskRisk.READ),
        ("修改 web/src/App.tsx 的发送按钮。", TaskIntent.FIX, TaskRisk.MUTATE),
        (
            "Describe RequestSnapshot without running commands or editing code.",
            TaskIntent.EXPLAIN,
            TaskRisk.READ,
        ),
    ],
)
def test_router_respects_negation_and_token_boundaries(
    goal: str,
    intent: TaskIntent,
    risk: TaskRisk,
) -> None:
    profile = TaskStrategyRouter().classify_rules(goal)
    expected_scope = TaskScope.READ_ONLY if risk == TaskRisk.READ else TaskScope.SINGLE_FILE

    assert profile.intent == intent
    assert profile.scope == expected_scope
    assert profile.risk == risk


# 功能：验证跨文件任务只有明确独立且不存在兼容耦合时才允许委派
# 设计：对比同一协议的耦合修改和无共享文件的并行修改，证明路由不会见到多文件就盲目启动 Worker
def test_router_delegates_only_independent_non_overlapping_work() -> None:
    router = TaskStrategyRouter()

    coupled = router.classify_rules(
        "同时修改 Python 编码器和 TypeScript 解码器，保持同一个 JSON 协议。"
    )
    independent = router.classify_rules(
        "分别重构互不依赖的缓存模块和日志模块，各自有独立测试，可以并行处理。"
    )

    assert coupled.scope == TaskScope.MULTI_FILE
    assert coupled.strategy == TaskStrategy.PLAN_FIRST
    assert not coupled.delegation_allowed
    assert independent.scope == TaskScope.MULTI_FILE
    assert independent.strategy == TaskStrategy.DELEGATE
    assert independent.delegation_allowed


# 功能：验证新增测试属于测试交付而修复后运行测试仍属于修复交付
# 设计：用相同 mutate 与 test 信号构造不同主目标，确保 Router 按用户交付物而非关键词优先级分类
def test_router_uses_primary_deliverable_for_fix_and_test_tasks() -> None:
    router = TaskStrategyRouter()

    add_test = router.classify_rules("给 tests/test_cache.py 增加一个并发回归测试。")
    fix_then_test = router.classify_rules("修复 src/cache.py 的过期判断并运行对应 pytest。")

    assert add_test.intent == TaskIntent.TEST
    assert add_test.risk == TaskRisk.MUTATE
    assert fix_then_test.intent == TaskIntent.FIX
    assert fix_then_test.risk == TaskRisk.SHELL


# 功能：验证读取源码并写出解释文档仍保持 explain 主意图但开放单文件写入
# 设计：复现 Benchmark 中“不要改参考代码但需产出报告”的冲突措辞，分离交付意图与最高权限风险
def test_explanation_artifact_is_classified_as_bounded_write() -> None:
    profile = TaskStrategyRouter().classify_rules(
        "阅读 reference/cache.py，在 report.md 中解释 race 和 lock；不要修改参考代码。"
    )

    assert profile.intent == TaskIntent.EXPLAIN
    assert profile.scope == TaskScope.SINGLE_FILE
    assert profile.risk == TaskRisk.MUTATE
    assert profile.strategy == TaskStrategy.DIRECT
    assert profile.model_tool_allowlist() == frozenset({"__all_except_delegation__"})


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


# 功能：验证实验单 Agent 基线可强制直接执行且不会保留委派能力
# 设计：先生成真实跨文件画像再只覆盖执行策略，断言风险范围保持而权限收窄
def test_experiment_direct_override_preserves_profile_and_disables_delegation() -> None:
    router = TaskStrategyRouter()
    profile = router.classify_rules("修改整个仓库的多个模块并运行测试")

    overridden = router.override_execution_strategy(profile, TaskStrategy.DIRECT)

    assert overridden.strategy == TaskStrategy.DIRECT
    assert not overridden.delegation_allowed
    assert overridden.risk == profile.risk
    assert overridden.scope == profile.scope
    assert overridden.digest != profile.digest


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


# 功能：验证委派策略只允许父 Agent 只读探索并一次性执行已校验 DAG
# 设计：检查顶层工具与 action 白名单，证明 Worker 失败后父 Agent 不能绕过 Worktree 直接修改
def test_delegate_profile_blocks_parent_mutation_and_manual_polling() -> None:
    profile = TaskStrategyRouter().classify_rules(
        "并行修改三个互不依赖的文件并分别验证"
    )

    assert profile.strategy == TaskStrategy.DELEGATE
    tools = profile.model_tool_allowlist() or frozenset()
    actions = profile.model_action_allowlist()
    assert "agent" in tools
    assert "Bash" not in tools
    assert "Run" not in tools
    assert actions["File"] == frozenset(
        {"read", "list", "search_name", "search_content"}
    )
    assert actions["agent"] == frozenset({"validate_plan", "execute_plan"})

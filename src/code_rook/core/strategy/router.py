from __future__ import annotations

import json
import re
from typing import Any

from code_rook.core.authority import RuntimeMode
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.strategy.models import (
    ContextPolicy,
    TaskIntent,
    TaskProfile,
    TaskRisk,
    TaskScope,
    TaskStrategy,
)

_READ_RE = re.compile(
    r"(?i)(解释|说明|分析|理解|检查|审查|查看|列出|定位|搜索|文件夹|目录|有哪些|是什么|为什么|"
    r"\b(?:explain|inspect|review|list|locate|search|read|describe|analy[sz]e|why|what)\b)"
)
_MUTATE_RE = re.compile(
    r"(?i)(修复|修改|实现|增加|新增|删除|重构|改造|补(?:充|齐|全)?|接入|迁移|记住|保存记忆|"
    r"\b(?:fix|change|implement|add|remove|refactor|update|integrate|migrate|edit|expand|remember)\b)"
)
_TEST_RE = re.compile(
    r"(?i)(测试|验证|诊断|pytest|mypy|ruff|\b(?:test|tests|testing|verify|lint|build|diagnostic|coverage)\b)"
)
_TEST_DELIVERABLE_RE = re.compile(
    r"(?i)(给.{0,40}(?:增加|新增|补充).{0,30}测试|"
    r"给.{0,40}测试.{0,20}(?:增加|新增|补充)|补齐.{0,20}测试|执行.{0,20}(ruff|mypy|pytest)|"
    r"运行.{0,30}(测试|pytest|mypy|ruff)|"
    r"\b(?:add|write|expand).{0,50}\b(?:test|tests|coverage)\b|"
    r"\b(?:run|execute).{0,30}\b(?:pytest|mypy|ruff|tests?|lint)\b)"
)
_FIX_PRIMARY_RE = re.compile(r"(?i)(修复|修好|\bfix\b)")
_SHELL_RE = re.compile(
    r"(?i)(命令|终端|运行|启动|安装|构建|提交|推送|执行.{0,20}(ruff|mypy|pytest)|"
    r"\b(?:shell|terminal|command|commands|run|execute|install|build|commit|push)\b)"
)
_EXTERNAL_RE = re.compile(
    r"(?i)(联网|网站|网页|浏览器|下载|官网|官方仓库|"
    r"github|http[s]?://|\b(?:website|online|internet|download|fetch)\b)"
)
_REFACTOR_RE = re.compile(
    r"(?i)(重构|架构调整|模块化|迁移.{0,20}(类型|命名|接口)|"
    r"\b(?:refactor|restructure|modularize|migrate)\b)"
)
_REPOSITORY_RE = re.compile(
    r"(?i)(整个项目|整个代码库|整个仓库|整个.{0,16}模块|所有调用方|全局|全仓|"
    r"接入.{0,60}(?:协议|文档)|"
    r"\b(?:repository|codebase|project.?wide|repo.?wide|throughout|"
    r"every consumer|all callers|subsystem)\b|"
    r"\bintegrat(?:e|ing).{0,80}\bacross\b)"
)
_MULTI_RE = re.compile(
    r"(?i)(多文件|跨文件|多个(?:文件|模块|组件)|配置.{0,20}(服务|加载器|调用方)|"
    r"编码器.{0,20}解码器|provider.{0,30}(协议|文档)|协议.{0,30}文档|"
    r"\b(?:multi.?file|multiple files?|modules|components|encoder.{0,30}decoder|"
    r"configuration.{0,40}(service|loader|consumer)|across)\b)"
)
_PARALLEL_RE = re.compile(
    r"(?i)(互不依赖|无共享文件|不重叠|独立(?:测试|验收)|可以并行|并行处理|"
    r"\b(?:independent|unrelated|no shared files?|non.?overlapping|in parallel|separate tests?)\b)"
)
_COUPLED_RE = re.compile(
    r"(?i)(保持同一个|同一(?:协议|接口|契约)|兼容|所有调用方|调用链|"
    r"\b(?:same contract|one .*contract|compatib|migration|all callers|every consumer|together)\b)"
)
_LONG_RE = re.compile(
    r"(?i)(长任务|完整实现|全部完成|不要停止|多轮|恢复|迁移|long.?running|"
    r"complete all|migration|resume)"
)
_FILE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/])?[A-Za-z0-9_.@+()-]+(?:[\\/][A-Za-z0-9_.@+()-]+)*\.[A-Za-z0-9]{1,10}"
)
_CONVERSATION_RE = re.compile(
    r"(?i)(你好|您好|你是谁|你是什么模型|什么模型|具体型号|你能做什么|你能干什么|"
    r"你会什么|有什么功能|怎么使用|如何使用|"
    r"who are you|what model|hello|how do i use|what can you do)"
)
_CHINESE_NEGATION_RE = re.compile(
    r"(?:不要|无需|不用|禁止|(?<!分)别|不可|只读(?:即可)?)\s*[^，。；;.!?]{0,16}$"
)
_ENGLISH_NEGATION_RE = re.compile(
    r"(?i)(?:do not|don't|dont|without|never|no need to)\s+(?:\w+[ -]?){0,4}$"
)

_SCOPE_RANK = {
    TaskScope.READ_ONLY: 0,
    TaskScope.SINGLE_FILE: 1,
    TaskScope.MULTI_FILE: 2,
    TaskScope.REPOSITORY: 3,
}
_RISK_RANK = {
    TaskRisk.READ: 0,
    TaskRisk.MUTATE: 1,
    TaskRisk.SHELL: 2,
    TaskRisk.EXTERNAL: 3,
}


class TaskStrategyRouter:
    # 初始化混合任务路由器并设置低置信度安全回退阈值
    def __init__(self, *, confidence_threshold: float = 0.75) -> None:
        self._confidence_threshold = confidence_threshold

    # 先执行确定性安全分类，存在歧义时至多调用一次模型分类并保守合并
    async def classify(
        self,
        goal: str,
        *,
        provider: LLMProvider | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        preset_id: str = "standard",
        bus: EventBus | None = None,
        run_id: str = "task-profile",
        method: str = "hybrid",
        delegation_policy: str = "routed",
    ) -> TaskProfile:
        rules = self.classify_rules(
            goal,
            runtime_mode=runtime_mode,
            preset_id=preset_id,
        )
        if method == "rules_only" or provider is None:
            return self._apply_delegation_policy(rules, delegation_policy)
        if method == "hybrid" and rules.confidence >= self._confidence_threshold:
            return self._apply_delegation_policy(rules, delegation_policy)
        model_profile = await self._classify_with_model(
            goal,
            provider=provider,
            bus=bus or EventBus(),
            run_id=run_id,
        )
        if method == "llm_only":
            selected = (
                model_profile
                if model_profile is not None
                else self._safe_fallback(rules, "model_invalid")
            )
            return self._apply_delegation_policy(selected, delegation_policy)
        if model_profile is None:
            return self._apply_delegation_policy(
                self._safe_fallback(rules, "model_invalid"),
                delegation_policy,
            )
        merged = self.merge(rules, model_profile)
        if merged.confidence < self._confidence_threshold:
            return self._safe_fallback(merged, "low_confidence")
        return self._apply_delegation_policy(merged, delegation_policy)

    # 使用可审计规则提取任务意图、范围、风险和长任务信号
    def classify_rules(
        self,
        goal: str,
        *,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        preset_id: str = "standard",
    ) -> TaskProfile:
        signals: list[str] = []
        read = _has_unnegated_match(_READ_RE, goal)
        mutate = _has_unnegated_match(_MUTATE_RE, goal)
        shell = _has_unnegated_match(_SHELL_RE, goal)
        external = _has_unnegated_match(_EXTERNAL_RE, goal)
        repository = bool(_REPOSITORY_RE.search(goal))
        parallel = bool(_PARALLEL_RE.search(goal))
        multi = repository or parallel or bool(_MULTI_RE.search(goal))
        coupled = bool(_COUPLED_RE.search(goal))
        files = sorted(set(_FILE_RE.findall(goal)))
        conversational = bool(_CONVERSATION_RE.search(goal)) and not (
            mutate
            or shell
            or external
            or files
        )
        if conversational:
            signals.append("conversation_answer")
        if read:
            signals.append("read_intent")
        if mutate:
            signals.append("mutation_intent")
        if shell:
            signals.append("shell_intent")
        if external:
            signals.append("external_intent")
        if multi:
            signals.append("multi_file_signal")
        if repository:
            signals.append("repository_scope_signal")
        if parallel:
            signals.append("independent_parallel_signal")
        if coupled:
            signals.append("coupled_change_signal")
        if len(files) > 1:
            multi = True
            signals.append("multiple_explicit_files")
        long_task = bool(_LONG_RE.search(goal)) or multi
        if long_task:
            signals.append("long_task_signal")

        if runtime_mode == RuntimeMode.PLAN:
            signals.append("explicit_plan_mode")
        if preset_id == "minimal":
            signals.append("minimal_preset")

        refactor = _has_unnegated_match(_REFACTOR_RE, goal)
        test_deliverable = bool(_TEST_DELIVERABLE_RE.search(goal))
        fix_primary = bool(_FIX_PRIMARY_RE.search(goal))
        if conversational:
            intent = TaskIntent.ANSWER
        elif refactor:
            intent = TaskIntent.REFACTOR
        elif parallel and multi and mutate:
            intent = TaskIntent.MULTI_FILE_CHANGE
        elif (test_deliverable and not fix_primary) or (_TEST_RE.search(goal) and not mutate):
            intent = TaskIntent.TEST
        elif multi and mutate:
            intent = TaskIntent.MULTI_FILE_CHANGE
        elif mutate:
            intent = TaskIntent.FIX
        elif read:
            intent = (
                TaskIntent.EXPLAIN
                if re.search(
                    r"(?i)(解释|说明|是什么|为什么|\b(?:explain|describe|why|what)\b)",
                    goal,
                )
                else TaskIntent.INSPECT
            )
        else:
            intent = TaskIntent.INSPECT

        if external:
            risk = TaskRisk.EXTERNAL
        elif shell:
            risk = TaskRisk.SHELL
        elif mutate:
            risk = TaskRisk.MUTATE
        else:
            risk = TaskRisk.READ

        if not mutate:
            scope = TaskScope.READ_ONLY
        elif repository:
            scope = TaskScope.REPOSITORY
        elif multi:
            scope = TaskScope.MULTI_FILE
        elif len(files) == 1:
            scope = TaskScope.SINGLE_FILE
        else:
            scope = TaskScope.SINGLE_FILE

        clear = read or mutate or shell or external or conversational
        confidence = 0.96 if conversational else 0.92 if clear else 0.55
        if read and mutate:
            confidence = 0.78
        delegation_allowed = bool(
            multi
            and mutate
            and parallel
            and not coupled
            and runtime_mode != RuntimeMode.PLAN
            and preset_id != "minimal"
        )
        if runtime_mode == RuntimeMode.PLAN or not clear:
            strategy = TaskStrategy.PLAN_FIRST
        elif conversational:
            strategy = TaskStrategy.DIRECT
        elif delegation_allowed:
            strategy = TaskStrategy.DELEGATE
        elif scope in {TaskScope.MULTI_FILE, TaskScope.REPOSITORY}:
            strategy = TaskStrategy.PLAN_FIRST
        else:
            strategy = TaskStrategy.DIRECT
        return TaskProfile(
            intent=intent,
            scope=scope,
            risk=risk,
            strategy=strategy,
            context_policy=(ContextPolicy.LONG_TASK if long_task else ContextPolicy.STANDARD),
            confidence=confidence,
            signals=tuple(signals),
            source="rules",
            delegation_allowed=delegation_allowed,
            deliverable=_deliverable_for(intent),
            success_criteria=_success_criteria_for(intent, risk),
            user_summary=_user_summary_for(intent, strategy, scope, risk, goal),
        ).with_digest()

    # 将模型语义分类与规则安全底座合并，风险和范围只允许提高
    def merge(self, rules: TaskProfile, model: TaskProfile) -> TaskProfile:
        risk = max((rules.risk, model.risk), key=_RISK_RANK.__getitem__)
        scope = max((rules.scope, model.scope), key=_SCOPE_RANK.__getitem__)
        delegation_allowed = (
            rules.delegation_allowed
            and model.delegation_allowed
            and scope in {TaskScope.MULTI_FILE, TaskScope.REPOSITORY}
        )
        strategy = model.strategy
        if not delegation_allowed and strategy == TaskStrategy.DELEGATE:
            strategy = TaskStrategy.PLAN_FIRST
        return TaskProfile(
            intent=model.intent,
            scope=scope,
            risk=risk,
            strategy=strategy,
            context_policy=(
                ContextPolicy.LONG_TASK
                if ContextPolicy.LONG_TASK in {rules.context_policy, model.context_policy}
                else ContextPolicy.STANDARD
            ),
            confidence=model.confidence,
            signals=tuple(dict.fromkeys((*rules.signals, *model.signals))),
            source="hybrid",
            delegation_allowed=delegation_allowed,
            deliverable=model.deliverable or rules.deliverable,
            success_criteria=model.success_criteria or rules.success_criteria,
            user_summary=model.user_summary
            or rules.user_summary
            or _user_summary_for(model.intent, strategy, scope, risk),
        ).with_digest()

    # 使用无工具短输出请求处理规则无法判断的语义歧义
    async def _classify_with_model(
        self,
        goal: str,
        *,
        provider: LLMProvider,
        bus: EventBus,
        run_id: str,
    ) -> TaskProfile | None:
        prompt = (
            "Classify this coding task. Return one compact JSON object only, under 256 tokens, "
            "with intent, scope, risk, strategy, context_policy, confidence, signals, "
            "delegation_allowed. Never lower stated shell/external risk. Task:\n" + goal
        )
        try:
            response = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tool_schemas=[],
                bus=bus,
                run_id=f"{run_id}:classifier",
                step=0,
                system="Return valid JSON only.",
                thinking="off",
            )
            raw = response.text.strip()
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                return None
            data: dict[str, Any] = json.loads(raw[start : end + 1])
            data["source"] = "llm"
            data["signals"] = tuple(str(value) for value in data.get("signals", []))
            data.pop("digest", None)
            return TaskProfile.model_validate(data).with_digest()
        except Exception:
            return None

    # 在模型无效或置信度不足时固定回退为先规划且禁止委派
    def _safe_fallback(self, profile: TaskProfile, reason: str) -> TaskProfile:
        return profile.model_copy(
            update={
                "strategy": TaskStrategy.PLAN_FIRST,
                "delegation_allowed": False,
                "signals": tuple((*profile.signals, reason, "safe_fallback")),
                "source": f"{profile.source}_fallback",
                "digest": "",
            }
        ).with_digest()

    # 将实验或生产委派策略应用到画像且不允许单 Agent 模式泄露委派工具
    def _apply_delegation_policy(
        self,
        profile: TaskProfile,
        policy: str,
    ) -> TaskProfile:
        if policy == "routed":
            return profile
        if policy == "single":
            return profile.model_copy(
                update={
                    "delegation_allowed": False,
                    "strategy": (
                        TaskStrategy.PLAN_FIRST
                        if profile.scope in {TaskScope.MULTI_FILE, TaskScope.REPOSITORY}
                        else TaskStrategy.DIRECT
                    ),
                    "signals": tuple((*profile.signals, "single_agent_policy")),
                    "digest": "",
                }
            ).with_digest()
        if policy == "always_delegate":
            allowed = profile.risk != TaskRisk.READ
            return profile.model_copy(
                update={
                    "delegation_allowed": allowed,
                    "strategy": TaskStrategy.DELEGATE if allowed else TaskStrategy.DIRECT,
                    "signals": tuple((*profile.signals, "always_delegate_policy")),
                    "digest": "",
                }
            ).with_digest()
        raise ValueError(f"unknown delegation policy: {policy}")

    # 为受控对照实验冻结执行策略且保持风险、范围和权限只收窄不扩大
    def override_execution_strategy(
        self,
        profile: TaskProfile,
        strategy: TaskStrategy,
    ) -> TaskProfile:
        delegation_allowed = profile.delegation_allowed and strategy == TaskStrategy.DELEGATE
        return profile.model_copy(
            update={
                "strategy": strategy,
                "delegation_allowed": delegation_allowed,
                "signals": tuple((*profile.signals, f"experiment_strategy_{strategy.value}")),
                "source": f"{profile.source}_experiment",
                "digest": "",
            }
        ).with_digest()


# 判断动作词是否没有被同一短语中的中英文否定词约束
def _has_unnegated_match(pattern: re.Pattern[str], text: str) -> bool:
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 64) : match.start()]
        boundary = max(prefix.rfind(mark) for mark in "，。；;.!?\n")
        clause_prefix = prefix[boundary + 1 :]
        if _CHINESE_NEGATION_RE.search(clause_prefix):
            continue
        if _ENGLISH_NEGATION_RE.search(clause_prefix):
            continue
        noun_context = re.search(
            r"(?:当前|相关|这个|现有|实际|具体|压缩|的)\s*$",
            clause_prefix,
        )
        if match.group(0) == "实现" and noun_context:
            continue
        return True
    return False


# 返回任务类型对应的默认可交付结果说明
def _deliverable_for(intent: TaskIntent) -> str:
    return {
        TaskIntent.ANSWER: "直接回答用户问题",
        TaskIntent.EXPLAIN: "基于代码证据的解释",
        TaskIntent.INSPECT: "带定位依据的审查结论",
        TaskIntent.FIX: "可审查的修复与验证结果",
        TaskIntent.REFACTOR: "保持行为兼容的重构与验证结果",
        TaskIntent.TEST: "可复现的测试或诊断结果",
        TaskIntent.MULTI_FILE_CHANGE: "跨文件变更、验证结果与风险说明",
    }[intent]


# 返回任务类型和风险对应的最低成功标准
def _success_criteria_for(intent: TaskIntent, risk: TaskRisk) -> tuple[str, ...]:
    criteria = ["结论与实际代码或命令结果一致"]
    if intent in {TaskIntent.FIX, TaskIntent.REFACTOR, TaskIntent.MULTI_FILE_CHANGE}:
        criteria.extend(("变更范围与用户目标一致", "报告实际运行过的验证"))
    elif intent == TaskIntent.TEST:
        criteria.append("测试命令和结果可复现")
    if risk in {TaskRisk.SHELL, TaskRisk.EXTERNAL}:
        criteria.append("高风险操作经过权限与沙箱管线")
    return tuple(criteria)


# 生成时间线中展示的一句话执行策略说明
def _user_summary_for(
    intent: TaskIntent,
    strategy: TaskStrategy,
    scope: TaskScope,
    risk: TaskRisk,
    goal: str = "",
) -> str:
    normalized_goal = " ".join(goal.split())
    target = normalized_goal[:42] + ("…" if len(normalized_goal) > 42 else "")
    subject = f"围绕“{target}”" if target else "围绕当前任务"
    if intent == TaskIntent.ANSWER:
        return "直接回答，不执行工具。"
    if strategy == TaskStrategy.DELEGATE:
        return f"{subject}确认可独立验收的子任务，再决定是否并行处理。"
    if strategy == TaskStrategy.PLAN_FIRST:
        if intent == TaskIntent.REFACTOR:
            return f"{subject}梳理依赖和兼容边界，确认计划后再重构。"
        if intent == TaskIntent.MULTI_FILE_CHANGE:
            return f"{subject}定位跨文件依赖，确认修改顺序后再实施。"
        return f"{subject}先定位相关实现，确认计划后再修改。"
    if scope == TaskScope.READ_ONLY or risk == TaskRisk.READ:
        return f"{subject}读取相关实现，并基于实际证据回答。"
    if intent == TaskIntent.TEST:
        return f"{subject}运行相关验证，定位失败后给出可复现结论。"
    return f"{subject}直接处理明确范围，并在结束前验证变更。"

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
    r"(?i)(解释|说明|分析|理解|检查|审查|查看|是什么|为什么|explain|inspect|review|analy[sz]e|why|what)"
)
_MUTATE_RE = re.compile(
    r"(?i)(修复|修改|实现|增加|删除|重构|改造|补全|fix|change|implement|add|remove|refactor|update)"
)
_TEST_RE = re.compile(r"(?i)(测试|验证|pytest|mypy|ruff|test|verify|lint|build)")
_SHELL_RE = re.compile(
    r"(?i)(命令|终端|运行|启动|安装|构建|提交|推送|shell|terminal|command|run|install|build|commit|push)"
)
_EXTERNAL_RE = re.compile(
    r"(?i)(联网|网站|网页|下载|api|github|http[s]?://|web|internet|download|fetch)"
)
_REFACTOR_RE = re.compile(r"(?i)(重构|架构|模块化|refactor|architecture|modular)")
_MULTI_RE = re.compile(
    r"(?i)(多文件|跨文件|整个项目|代码库|仓库|全局|模块|multi.?file|repository|codebase|project.?wide|modules?)"
)
_LONG_RE = re.compile(
    r"(?i)(长任务|完整实现|全部完成|不要停止|多轮|恢复|迁移|long.?running|"
    r"complete all|migration|resume)"
)
_FILE_RE = re.compile(r"(?:[A-Za-z]:[\\/])?[\w.@+() -]+(?:[\\/][\w.@+() -]+)*\.[A-Za-z0-9]{1,10}")

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
        read = bool(_READ_RE.search(goal))
        mutate = bool(_MUTATE_RE.search(goal))
        shell = bool(_SHELL_RE.search(goal))
        external = bool(_EXTERNAL_RE.search(goal))
        multi = bool(_MULTI_RE.search(goal))
        files = sorted(set(_FILE_RE.findall(goal)))
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

        if _REFACTOR_RE.search(goal):
            intent = TaskIntent.REFACTOR
        elif _TEST_RE.search(goal) and not mutate:
            intent = TaskIntent.TEST
        elif multi and mutate:
            intent = TaskIntent.MULTI_FILE_CHANGE
        elif mutate:
            intent = TaskIntent.FIX
        elif read:
            intent = (
                TaskIntent.EXPLAIN
                if re.search(r"(?i)(解释|说明|是什么|为什么|explain|why|what)", goal)
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
        elif multi:
            scope = (
                TaskScope.REPOSITORY
                if re.search(
                    r"(?i)(整个项目|代码库|仓库|全局|repository|codebase|project.?wide)",
                    goal,
                )
                else TaskScope.MULTI_FILE
            )
        elif len(files) == 1:
            scope = TaskScope.SINGLE_FILE
        else:
            scope = TaskScope.SINGLE_FILE

        clear = read or mutate or shell or external
        confidence = 0.92 if clear else 0.55
        if read and mutate:
            confidence = 0.78
        delegation_allowed = (
            multi and mutate and runtime_mode != RuntimeMode.PLAN and preset_id != "minimal"
        )
        if runtime_mode == RuntimeMode.PLAN or not clear:
            strategy = TaskStrategy.PLAN_FIRST
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

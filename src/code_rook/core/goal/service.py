from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import JsonValue

from code_rook.core.goal.models import (
    CompletionEvidence,
    GoalRecord,
    GoalStatus,
    GoalTimelineEntry,
)
from code_rook.core.goal.store import GoalStore


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class GoalService:
    # 初始化持久 Goal 控制面
    def __init__(self, store: GoalStore) -> None:
        self._store = store

    # 追加 Goal timeline 并更新修改时间
    def _record(
        self,
        goal: GoalRecord,
        event: str,
        actor: str,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        now = _now()
        goal.timeline.append(
            GoalTimelineEntry(
                seq=len(goal.timeline) + 1,
                event=event,
                actor=actor.strip() or "user",
                at=now,
                details=details or {},
            )
        )
        goal.updated_at = now

    # 创建与 Task/Plan 分离的用户级 Goal
    def create(
        self,
        objective: str,
        *,
        token_budget: int | None = None,
        constraints: list[str] | None = None,
        actor: str = "user",
    ) -> GoalRecord:
        clean_objective = objective.strip()
        if not clean_objective:
            raise ValueError("goal objective must not be empty")
        now = _now()
        goal = GoalRecord(
            id=f"goal-{uuid.uuid4().hex[:12]}",
            objective=clean_objective,
            token_budget=token_budget,
            constraints=[item.strip() for item in constraints or [] if item.strip()],
            created_at=now,
            updated_at=now,
        )
        self._record(goal, "goal.created", actor, {"status": "active"})
        self._store.save(goal)
        return goal

    # 返回指定 Goal
    def get(self, goal_id: str) -> GoalRecord:
        return self._store.get(goal_id)

    # 稳定列出全部 Goal
    def list_all(self) -> list[GoalRecord]:
        return self._store.list()

    # 累加跨 turn token 和 elapsed 使用量，禁止超过已声明预算
    def add_usage(
        self,
        goal_id: str,
        *,
        tokens: int,
        elapsed_ms: int,
        actor: str = "system",
    ) -> GoalRecord:
        if tokens < 0 or elapsed_ms < 0:
            raise ValueError("goal usage increments must be non-negative")

        # 原子累加预算使用并记录 timeline
        def mutate(goal: GoalRecord) -> GoalRecord:
            next_tokens = goal.tokens_used + tokens
            if goal.token_budget is not None and next_tokens > goal.token_budget:
                raise ValueError(f"goal token budget exceeded: {goal.token_budget}")
            goal.tokens_used = next_tokens
            goal.elapsed_ms += elapsed_ms
            self._record(
                goal,
                "goal.usage_updated",
                actor,
                {"tokens_used": goal.tokens_used, "elapsed_ms": goal.elapsed_ms},
            )
            return goal

        return self._store.mutate(goal_id, mutate)
    # 将执行 Task 关联到 Goal，保持 ID 去重和稳定顺序
    def link_task(
        self,
        goal_id: str,
        task_id: int,
        *,
        actor: str = "agent",
    ) -> GoalRecord:
        # 原子追加 task 引用并写入 timeline
        def mutate(goal: GoalRecord) -> GoalRecord:
            if task_id not in goal.linked_task_ids:
                goal.linked_task_ids.append(task_id)
                self._record(goal, "goal.task_linked", actor, {"task_id": task_id})
            return goal

        return self._store.mutate(goal_id, mutate)

    # 追加可追溯完成证据
    def add_evidence(
        self,
        goal_id: str,
        *,
        kind: str,
        reference: str,
        summary: str = "",
        actor: str = "agent",
    ) -> GoalRecord:
        # 原子追加 evidence 并记录引用而不复制产物正文
        def mutate(goal: GoalRecord) -> GoalRecord:
            evidence = CompletionEvidence(
                kind=kind,
                reference=reference,
                summary=summary,
                recorded_at=_now(),
            )
            goal.completion_evidence.append(evidence)
            self._record(
                goal,
                "goal.evidence_added",
                actor,
                {"kind": evidence.kind, "reference": evidence.reference},
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 设置 Goal 终态；completed 必须存在至少一条可验证证据
    def set_status(
        self,
        goal_id: str,
        status: GoalStatus,
        *,
        actor: str = "user",
    ) -> GoalRecord:
        # 原子校验并更新 Goal 状态
        def mutate(goal: GoalRecord) -> GoalRecord:
            if status == "completed" and not goal.completion_evidence:
                raise ValueError("completed goal requires completion evidence")
            goal.status = status
            self._record(goal, "goal.status_changed", actor, {"status": status})
            return goal

        return self._store.mutate(goal_id, mutate)

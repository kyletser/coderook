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

_NONTERMINAL_STATUSES: set[GoalStatus] = {"active", "paused", "blocked"}


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
        session_id: str,
        token_budget: int | None = None,
        constraints: list[str] | None = None,
        completion_criteria: list[str] | None = None,
        actor: str = "user",
    ) -> GoalRecord:
        clean_objective = objective.strip()
        if not clean_objective:
            raise ValueError("goal objective must not be empty")
        clean_session_id = session_id.strip()
        if not clean_session_id:
            raise ValueError("goal session_id must not be empty")
        current = self.current(clean_session_id)
        if current is not None:
            raise ValueError(f"session already has an unfinished goal: {current.id}")
        now = _now()
        goal = GoalRecord(
            id=f"goal-{uuid.uuid4().hex[:12]}",
            session_id=clean_session_id,
            objective=clean_objective,
            token_budget=token_budget,
            constraints=[item.strip() for item in constraints or [] if item.strip()],
            completion_criteria=[
                item.strip() for item in completion_criteria or [] if item.strip()
            ],
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
        return self._store.list_all()

    # daemon 启动时把遗留 active run 恢复为 blocked，避免永久占用 Goal
    def recover_interrupted(self) -> list[GoalRecord]:
        recovered: list[GoalRecord] = []
        for record in self.list_all():
            if record.current_run_id is None or record.status not in _NONTERMINAL_STATUSES:
                continue

            # 原子清除遗留 run 并记录重启恢复原因
            def mutate(goal: GoalRecord) -> GoalRecord:
                interrupted_run_id = goal.current_run_id or ""
                goal.current_run_id = None
                goal.status = "blocked"
                goal.status_reason = "daemon restarted during an active run"
                self._record(
                    goal,
                    "goal.run_recovered",
                    "system",
                    {"run_id": interrupted_run_id, "reason": "daemon_restart"},
                )
                return goal

            recovered.append(self._store.mutate(record.id, mutate))
        return recovered

    # 返回 session 当前未终结 Goal，不存在时返回 None
    def current(self, session_id: str) -> GoalRecord | None:
        candidates = [
            goal
            for goal in self._store.list_for_session(session_id)
            if goal.status in _NONTERMINAL_STATUSES
        ]
        return candidates[-1] if candidates else None

    # 按 ID 或 session 定位 Goal，并要求二者至少提供一个
    def resolve(
        self,
        *,
        goal_id: str = "",
        session_id: str = "",
    ) -> GoalRecord:
        if goal_id.strip():
            return self.get(goal_id.strip())
        if session_id.strip():
            goal = self.current(session_id.strip())
            if goal is not None:
                return goal
            raise ValueError(f"session has no unfinished goal: {session_id.strip()}")
        raise ValueError("goal_id or session_id is required")

    # 修改未终结 Goal 的目标文本与可选完成标准
    def edit(
        self,
        goal_id: str,
        objective: str,
        *,
        completion_criteria: list[str] | None = None,
        actor: str = "user",
    ) -> GoalRecord:
        clean_objective = objective.strip()
        if not clean_objective:
            raise ValueError("goal objective must not be empty")

        # 原子修改目标并保留完整 timeline
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.status not in _NONTERMINAL_STATUSES:
                raise ValueError(f"cannot edit {goal.status} goal")
            goal.objective = clean_objective
            if completion_criteria is not None:
                goal.completion_criteria = [
                    item.strip() for item in completion_criteria if item.strip()
                ]
            self._record(goal, "goal.edited", actor, {"objective": clean_objective})
            return goal

        return self._store.mutate(goal_id, mutate)

    # 暂停未终结 Goal，后续 run 不再注入该目标
    def pause(self, goal_id: str, *, actor: str = "user") -> GoalRecord:
        return self._transition(goal_id, "paused", actor=actor)

    # 将暂停或阻塞 Goal 恢复为 active
    def resume(self, goal_id: str, *, actor: str = "user") -> GoalRecord:
        return self._transition(goal_id, "active", actor=actor)

    # 清除未终结 Goal，并保留审计记录而非删除文件
    def clear(self, goal_id: str, *, actor: str = "user") -> GoalRecord:
        return self._transition(goal_id, "cleared", actor=actor)

    # 校验 Goal 状态机后执行状态迁移
    def _transition(
        self,
        goal_id: str,
        status: GoalStatus,
        *,
        actor: str,
    ) -> GoalRecord:
        allowed: dict[GoalStatus, set[GoalStatus]] = {
            "active": {"paused", "blocked"},
            "paused": {"active", "blocked"},
            "blocked": {"active", "paused"},
            "cleared": _NONTERMINAL_STATUSES,
            "completed": {"active", "blocked"},
        }

        # 原子校验来源状态并记录迁移
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.status not in allowed[status]:
                raise ValueError(f"cannot change {goal.status} goal to {status}")
            goal.status = status
            goal.status_reason = {
                "active": "",
                "paused": "paused by user",
                "cleared": "cleared by user",
            }.get(status, goal.status_reason)
            if status == "cleared":
                goal.current_run_id = None
            self._record(goal, "goal.status_changed", actor, {"status": status})
            return goal

        return self._store.mutate(goal_id, mutate)

    # 关联新 run，并确保只有 active Goal 可以继续执行
    def start_run(
        self,
        goal_id: str,
        run_id: str,
        *,
        actor: str = "system",
    ) -> GoalRecord:
        # 原子登记当前 run 与稳定历史引用
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.status != "active":
                raise ValueError(f"cannot start run for {goal.status} goal")
            clean_run_id = run_id.strip()
            if not clean_run_id:
                raise ValueError("run_id must not be empty")
            if goal.current_run_id and goal.current_run_id != clean_run_id:
                raise ValueError(f"goal already has an active run: {goal.current_run_id}")
            if clean_run_id not in goal.linked_run_ids:
                goal.linked_run_ids.append(clean_run_id)
            goal.current_run_id = clean_run_id
            self._record(goal, "goal.run_started", actor, {"run_id": clean_run_id})
            return goal

        return self._store.mutate(goal_id, mutate)

    # 根据 run 结果记录本轮进度，失败时阻塞但不把单轮成功误判为整个 Goal 完成
    def finish_run(
        self,
        goal_id: str,
        run_id: str,
        *,
        succeeded: bool,
        reason: str = "",
        actor: str = "system",
    ) -> GoalRecord:
        # 原子落盘 run 结果，尊重用户已执行的暂停或清除
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.current_run_id not in {None, run_id}:
                raise ValueError(f"run is not active for goal: {run_id}")
            started = next(
                (
                    entry
                    for entry in reversed(goal.timeline)
                    if entry.event == "goal.run_started"
                    and entry.details.get("run_id") == run_id
                ),
                None,
            )
            if started is not None:
                try:
                    began_at = datetime.fromisoformat(started.at)
                    goal.elapsed_ms += max(
                        0,
                        int((datetime.now(UTC) - began_at).total_seconds() * 1000),
                    )
                except ValueError:
                    pass
            goal.current_run_id = None
            if goal.status in {"paused", "cleared"}:
                self._record(
                    goal,
                    "goal.run_stopped",
                    actor,
                    {"run_id": run_id, "reason": reason or "cancelled"},
                )
                return goal
            if not succeeded and goal.status != "completed":
                goal.status = "blocked"
                goal.status_reason = reason or "agent run failed"
            self._record(
                goal,
                "goal.run_finished",
                actor,
                {
                    "run_id": run_id,
                    "status": goal.status,
                    "reason": reason,
                },
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 使用明确引用的证据原子完成 Goal，避免“模型正常返回”被当成目标验收通过
    def complete(
        self,
        goal_id: str,
        *,
        evidence: list[tuple[str, str]],
        summary: str = "",
        actor: str = "agent",
    ) -> GoalRecord:
        clean_evidence = [
            (kind.strip(), reference.strip())
            for kind, reference in evidence
            if kind.strip() and reference.strip()
        ]
        if not clean_evidence:
            raise ValueError("completed goal requires completion evidence")

        # 在一次存储事务中追加证据并切换终态，防止中途失败留下半完成状态
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.status not in _NONTERMINAL_STATUSES:
                raise ValueError(f"cannot complete {goal.status} goal")
            recorded_at = _now()
            for kind, reference in clean_evidence:
                goal.completion_evidence.append(
                    CompletionEvidence(
                        kind=kind,
                        reference=reference,
                        summary=summary.strip(),
                        recorded_at=recorded_at,
                    )
                )
            goal.status = "completed"
            goal.status_reason = ""
            self._record(
                goal,
                "goal.completed",
                actor,
                {
                    "evidence_count": len(clean_evidence),
                    "summary": summary.strip(),
                },
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 记录真实模型用量；单次调用越界时保留实际值并自动暂停 Goal
    def record_usage(
        self,
        goal_id: str,
        *,
        tokens: int,
        actor: str = "system",
    ) -> GoalRecord:
        if tokens < 0:
            raise ValueError("goal usage increment must be non-negative")

        # 原子累计真实用量，并在预算耗尽时切换为 paused
        def mutate(goal: GoalRecord) -> GoalRecord:
            goal.tokens_used += tokens
            exhausted = (
                goal.token_budget is not None and goal.tokens_used >= goal.token_budget
            )
            if exhausted and goal.status == "active":
                goal.status = "paused"
                goal.status_reason = "token budget exhausted"
            self._record(
                goal,
                "goal.budget_exhausted" if exhausted else "goal.usage_updated",
                actor,
                {
                    "tokens_used": goal.tokens_used,
                    "token_budget": goal.token_budget,
                },
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 生成每轮注入系统提示的持久 Goal 上下文
    def render_context(self, goal: GoalRecord) -> str:
        lines = [
            f"Goal ID: {goal.id}",
            f"Objective: {goal.objective}",
            f"Status: {goal.status}",
        ]
        if goal.constraints:
            lines.append("Constraints:\n- " + "\n- ".join(goal.constraints))
        if goal.completion_criteria:
            lines.append(
                "Completion criteria:\n- " + "\n- ".join(goal.completion_criteria)
            )
        if goal.token_budget is not None:
            lines.append(
                f"Token budget: {goal.tokens_used}/{goal.token_budget} used"
            )
        lines.append(
            "Continue working toward this durable goal. Do not claim completion until "
            "the objective and every completion criterion are verified with evidence. "
            "When verification is complete, call update_goal with status=completed and "
            "concrete evidence references. If progress requires user input or cannot continue "
            "safely, call update_goal with status=blocked and explain the blocker."
        )
        return "\n".join(lines)

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
        reason: str = "",
        actor: str = "user",
    ) -> GoalRecord:
        # 原子校验并更新 Goal 状态
        def mutate(goal: GoalRecord) -> GoalRecord:
            if status == "completed" and not goal.completion_evidence:
                raise ValueError("completed goal requires completion evidence")
            goal.status = status
            goal.status_reason = reason.strip() if status == "blocked" else ""
            self._record(
                goal,
                "goal.status_changed",
                actor,
                {"status": status, "reason": goal.status_reason},
            )
            return goal

        return self._store.mutate(goal_id, mutate)

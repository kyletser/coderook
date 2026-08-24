from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from pydantic import JsonValue

from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    WorkspaceTrust,
)
from code_rook.core.goal.models import (
    CompletionEvidence,
    GoalContinueDecision,
    GoalRecord,
    GoalStatus,
    GoalTimelineEntry,
)
from code_rook.core.goal.store import GoalStore

_NONTERMINAL_STATUSES: set[GoalStatus] = {"active", "paused", "blocked"}
_MODE_RANK = {
    RuntimeMode.PLAN: 0,
    RuntimeMode.ACT: 1,
    RuntimeMode.OPERATE: 2,
}
_PROFILE_RANK = {
    AuthorityProfile.ASK: 0,
    AuthorityProfile.AUTO_REVIEW: 1,
    AuthorityProfile.FULL_ACCESS: 2,
}
_TRUST_RANK = {
    WorkspaceTrust.UNTRUSTED: 0,
    WorkspaceTrust.TRUSTED: 1,
}
_PAUSE_MESSAGES = {
    "completion_evidence_requires_acceptance": (
        "completion evidence requires verification or explicit user acceptance"
    ),
    "token_budget_exhausted": "token budget exhausted",
    "token_usage_unavailable": "token usage unavailable; automatic execution paused",
    "max_auto_turns_reached": "automatic turn limit reached",
    "max_wall_seconds_reached": "automatic wall-clock limit reached",
    "wall_clock_unavailable": "automatic wall-clock window is invalid",
    "permission_ceiling_exceeded": "current authority exceeds the goal permission ceiling",
    "daemon_restart_confirmation_required": (
        "daemon restarted; automatic continuation requires user confirmation"
    ),
}
_VERIFIED_EVIDENCE_KIND = "verified-run"
_LATEST_VERIFICATION_REFERENCE = "latest-verification"


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 把持久化时间戳解析为带 UTC 时区的 datetime，非法值返回 None
def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# 判断当前 authority 是否越过 Goal 创建时冻结的权限上限
def _authority_exceeds_ceiling(
    current: AuthoritySnapshot,
    ceiling: AuthoritySnapshot,
) -> bool:
    return (
        _MODE_RANK[current.mode] > _MODE_RANK[ceiling.mode]
        or _PROFILE_RANK[current.profile] > _PROFILE_RANK[ceiling.profile]
        or _TRUST_RANK[current.workspace_trust] > _TRUST_RANK[ceiling.workspace_trust]
        or not current.allowed_actions.issubset(ceiling.allowed_actions)
    )


# 统计 Goal 当前由模型调用 lease 原子预留但尚未结算的 token
def _reserved_tokens(goal: GoalRecord) -> int:
    return sum(max(0, value) for value in goal.token_reservations.values())


# 返回已由可信验证证据覆盖的完成标准集合
def _covered_criteria(goal: GoalRecord) -> set[str]:
    declared = {criterion for criterion in goal.completion_criteria}
    return {
        criterion
        for evidence in goal.completion_evidence
        if evidence.kind == _VERIFIED_EVIDENCE_KIND
        for criterion in evidence.covered_criteria
        if criterion in declared
    }


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
        auto_continue: bool = True,
        max_auto_turns: int = 3,
        max_wall_seconds: int = 1800,
        permission_ceiling: AuthoritySnapshot | None = None,
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
            auto_continue=auto_continue,
            max_auto_turns=max_auto_turns,
            max_wall_seconds=max_wall_seconds,
            auto_window_started_at=now if auto_continue else None,
            permission_ceiling=permission_ceiling or AuthoritySnapshot(),
            constraints=[item.strip() for item in constraints or [] if item.strip()],
            completion_criteria=[
                item.strip() for item in completion_criteria or [] if item.strip()
            ],
            created_at=now,
            updated_at=now,
        )
        self._record(
            goal,
            "goal.created",
            actor,
            {
                "status": "active",
                "auto_continue": auto_continue,
                "max_auto_turns": max_auto_turns,
                "max_wall_seconds": max_wall_seconds,
            },
        )
        self._store.save(goal)
        return goal

    # 返回指定 Goal
    def get(self, goal_id: str) -> GoalRecord:
        return self._store.get(goal_id)

    # 稳定列出全部 Goal
    def list_all(self) -> list[GoalRecord]:
        return self._store.list_all()

    # daemon 启动时暂停全部自动 Goal，普通遗留 active run 则恢复为 blocked
    def recover_interrupted(self) -> list[GoalRecord]:
        recovered: list[GoalRecord] = []
        for record in self.list_all():
            if record.status not in _NONTERMINAL_STATUSES:
                continue
            if (
                record.auto_continue
                and record.current_run_id is None
                and record.status == "paused"
                and record.paused_needs_confirmation
                and record.paused_reason == "daemon_restart_confirmation_required"
            ):
                continue
            if not record.auto_continue and record.current_run_id is None:
                continue

            # 原子清除遗留 run，并让自动模式等待用户重新确认其有限执行窗口
            def mutate(goal: GoalRecord) -> GoalRecord:
                interrupted_run_id = goal.current_run_id or ""
                goal.current_run_id = None
                abandoned_tokens = _reserved_tokens(goal)
                if abandoned_tokens:
                    goal.tokens_used += abandoned_tokens
                    if goal.token_budget is not None:
                        goal.tokens_used = min(goal.tokens_used, goal.token_budget)
                    goal.token_reservations.clear()
                if goal.auto_continue:
                    goal.status = "paused"
                    goal.paused_reason = "daemon_restart_confirmation_required"
                    goal.paused_needs_confirmation = True
                    goal.status_reason = _PAUSE_MESSAGES[goal.paused_reason]
                    event = "goal.auto_paused_after_restart"
                else:
                    goal.status = "blocked"
                    goal.status_reason = "daemon restarted during an active run"
                    goal.paused_reason = ""
                    goal.paused_needs_confirmation = False
                    event = "goal.run_recovered"
                self._record(
                    goal,
                    event,
                    "system",
                    {
                        "run_id": interrupted_run_id,
                        "reason": "daemon_restart",
                        "abandoned_reserved_tokens": abandoned_tokens,
                    },
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
            if status == "active":
                goal.paused_reason = ""
                goal.paused_needs_confirmation = False
                if goal.auto_continue:
                    goal.auto_turns_used = 0
                    goal.auto_window_started_at = _now()
            elif status == "paused":
                goal.paused_reason = "paused_by_user"
                goal.paused_needs_confirmation = False
            else:
                goal.paused_reason = ""
                goal.paused_needs_confirmation = False
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
        current_authority: AuthoritySnapshot | None = None,
        actor: str = "system",
    ) -> GoalRecord:
        clean_run_id = run_id.strip()
        if not clean_run_id:
            raise ValueError("run_id must not be empty")

        # 原子登记当前 run 与稳定历史引用
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.status != "active":
                raise ValueError(f"cannot start run for {goal.status} goal")
            if goal.current_run_id and goal.current_run_id != clean_run_id:
                raise ValueError(f"goal already has an active run: {goal.current_run_id}")
            is_new_auto_run = (
                goal.auto_continue and clean_run_id not in goal.linked_run_ids
            )
            if is_new_auto_run:
                decided_at = datetime.now(UTC)
                if goal.linked_run_ids:
                    continue_reason, wall_elapsed = self._continue_reason(
                        goal,
                        current_authority=current_authority or goal.permission_ceiling,
                        decided_at=decided_at,
                    )
                else:
                    wall_elapsed_value = self._wall_elapsed_seconds(goal, decided_at)
                    wall_elapsed = wall_elapsed_value or 0
                    if wall_elapsed_value is None:
                        continue_reason = "wall_clock_unavailable"
                    elif wall_elapsed_value >= goal.max_wall_seconds:
                        continue_reason = "max_wall_seconds_reached"
                    elif _authority_exceeds_ceiling(
                        current_authority or goal.permission_ceiling,
                        goal.permission_ceiling,
                    ):
                        continue_reason = "permission_ceiling_exceeded"
                    elif (
                        goal.token_budget is not None
                        and goal.tokens_used + _reserved_tokens(goal)
                        >= goal.token_budget
                    ):
                        continue_reason = "token_budget_exhausted"
                    else:
                        continue_reason = "ready_for_bounded_continuation"
                if continue_reason in _PAUSE_MESSAGES:
                    goal.status = "paused"
                    goal.paused_reason = continue_reason
                    goal.paused_needs_confirmation = True
                    goal.status_reason = _PAUSE_MESSAGES[continue_reason]
                    self._record(
                        goal,
                        "goal.auto_paused",
                        actor,
                        {
                            "reason": continue_reason,
                            "auto_turns_used": goal.auto_turns_used,
                            "tokens_used": goal.tokens_used,
                            "wall_elapsed_seconds": wall_elapsed,
                        },
                    )
                    return goal
                if continue_reason != "ready_for_bounded_continuation":
                    raise ValueError(
                        f"goal continuation is unavailable: {continue_reason}"
                    )
                goal.auto_turns_used += 1
            if clean_run_id not in goal.linked_run_ids:
                goal.linked_run_ids.append(clean_run_id)
            goal.current_run_id = clean_run_id
            self._record(
                goal,
                "goal.run_started",
                actor,
                {
                    "run_id": clean_run_id,
                    "auto_turns_used": goal.auto_turns_used,
                },
            )
            return goal

        updated = self._store.mutate(goal_id, mutate)
        if updated.current_run_id != clean_run_id:
            raise ValueError(
                "goal continuation requires user confirmation: "
                f"{updated.paused_reason or updated.status}"
            )
        return updated

    # 根据 run 结果记录本轮进度，失败时阻塞但不把单轮成功误判为整个 Goal 完成
    def finish_run(
        self,
        goal_id: str,
        run_id: str,
        *,
        succeeded: bool,
        reason: str = "",
        transient_failure: bool = False,
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
            if reason == "max_wall_seconds_reached" and goal.auto_continue:
                goal.status = "paused"
                goal.paused_reason = reason
                goal.paused_needs_confirmation = True
                goal.status_reason = _PAUSE_MESSAGES[reason]
            elif (
                not succeeded
                and goal.status != "completed"
                and not (goal.auto_continue and transient_failure)
            ):
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
                    "transient_failure": transient_failure,
                },
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 在 runner 尚未启动的持久化失败路径中原子释放 run 预留并阻塞 Goal
    def abort_run(
        self,
        goal_id: str,
        run_id: str,
        *,
        reason: str,
        actor: str = "system",
    ) -> GoalRecord:
        clean_reason = reason.strip() or "turn preparation failed"

        # 只回滚仍由同一 run 持有的预留，避免覆盖用户并发暂停、清除或完成
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.current_run_id not in {None, run_id}:
                raise ValueError(f"run is not active for goal: {run_id}")
            goal.current_run_id = None
            if goal.status == "active":
                goal.status = "blocked"
                goal.status_reason = clean_reason
            self._record(
                goal,
                "goal.run_aborted",
                actor,
                {"run_id": run_id, "reason": clean_reason},
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 计算当前自动执行窗口已消耗的墙钟秒数，非法锚点返回 None
    def _wall_elapsed_seconds(
        self,
        goal: GoalRecord,
        decided_at: datetime,
    ) -> int | None:
        started_at = _parse_time(goal.auto_window_started_at)
        if started_at is None:
            return None
        return max(0, int((decided_at - started_at).total_seconds()))

    # 返回自动执行窗口剩余墙钟秒数，供 SessionManager 对单个 Turn 设置硬 deadline
    def remaining_wall_seconds(
        self,
        goal_id: str,
        *,
        now: datetime | None = None,
    ) -> float | None:
        goal = self.get(goal_id)
        if not goal.auto_continue:
            return None
        started_at = _parse_time(goal.auto_window_started_at)
        if started_at is None:
            return 0.0
        deadline = started_at + timedelta(seconds=goal.max_wall_seconds)
        return max(0.0, (deadline - (now or datetime.now(UTC))).total_seconds())

    # 依次检查完成、预算、轮次、墙钟和权限边界并返回稳定原因码
    def _continue_reason(
        self,
        goal: GoalRecord,
        *,
        current_authority: AuthoritySnapshot,
        decided_at: datetime,
    ) -> tuple[str, int]:
        wall_elapsed = self._wall_elapsed_seconds(goal, decided_at)
        safe_wall_elapsed = wall_elapsed if wall_elapsed is not None else 0
        if not goal.auto_continue:
            return "auto_continue_disabled", safe_wall_elapsed
        if goal.status == "completed":
            return "goal_completed", safe_wall_elapsed
        if goal.status != "active":
            return (
                goal.paused_reason or f"goal_{goal.status}",
                safe_wall_elapsed,
            )
        if goal.current_run_id is not None:
            return "run_in_progress", safe_wall_elapsed
        covered = _covered_criteria(goal)
        if goal.completion_criteria and covered == set(goal.completion_criteria):
            return "completion_evidence_requires_acceptance", safe_wall_elapsed
        if (
            goal.token_budget is not None
            and goal.tokens_used + _reserved_tokens(goal) >= goal.token_budget
        ):
            if goal.token_reservations:
                return "token_budget_reserved", safe_wall_elapsed
            return "token_budget_exhausted", safe_wall_elapsed
        if goal.auto_turns_used >= goal.max_auto_turns:
            return "max_auto_turns_reached", safe_wall_elapsed
        if wall_elapsed is None:
            return "wall_clock_unavailable", safe_wall_elapsed
        if wall_elapsed >= goal.max_wall_seconds:
            return "max_wall_seconds_reached", wall_elapsed
        if _authority_exceeds_ceiling(current_authority, goal.permission_ceiling):
            return "permission_ceiling_exceeded", wall_elapsed
        return "ready_for_bounded_continuation", wall_elapsed

    # 根据当前 Goal 快照构造 typed 继续决策，不把普通成功误判为完成
    def _build_continue_decision(
        self,
        goal: GoalRecord,
        *,
        reason: str,
        wall_elapsed_seconds: int,
        decided_at: datetime,
    ) -> GoalContinueDecision:
        remaining_tokens = (
            None
            if goal.token_budget is None
            else max(
                0,
                goal.token_budget - goal.tokens_used - _reserved_tokens(goal),
            )
        )
        return GoalContinueDecision(
            goal_id=goal.id,
            session_id=goal.session_id,
            should_continue=reason == "ready_for_bounded_continuation",
            reason=reason,
            auto_turns_used=goal.auto_turns_used,
            remaining_auto_turns=max(0, goal.max_auto_turns - goal.auto_turns_used),
            tokens_used=goal.tokens_used,
            token_budget=goal.token_budget,
            remaining_tokens=remaining_tokens,
            wall_elapsed_seconds=wall_elapsed_seconds,
            max_wall_seconds=goal.max_wall_seconds,
            permission_ceiling=goal.permission_ceiling,
            paused_needs_confirmation=goal.paused_needs_confirmation,
            decided_at=decided_at.isoformat(),
        )

    # 原子评估下一自动轮，越界时持久化为需要用户确认的暂停态
    def decide_continue(
        self,
        goal_id: str,
        *,
        current_authority: AuthoritySnapshot | None = None,
        now: datetime | None = None,
        actor: str = "system",
    ) -> GoalContinueDecision:
        decided_at = now or datetime.now(UTC)
        if decided_at.tzinfo is None:
            decided_at = decided_at.replace(tzinfo=UTC)
        else:
            decided_at = decided_at.astimezone(UTC)
        goal = self.get(goal_id)
        authority = current_authority or goal.permission_ceiling
        reason, wall_elapsed = self._continue_reason(
            goal,
            current_authority=authority,
            decided_at=decided_at,
        )
        if reason in _PAUSE_MESSAGES and goal.status == "active":

            # 在存储锁内重算安全条件，防止并发完成或暂停后被旧决策覆盖
            def mutate(current: GoalRecord) -> GoalRecord:
                fresh_reason, _fresh_elapsed = self._continue_reason(
                    current,
                    current_authority=authority,
                    decided_at=decided_at,
                )
                if fresh_reason not in _PAUSE_MESSAGES or current.status != "active":
                    return current
                current.status = "paused"
                current.paused_reason = fresh_reason
                current.paused_needs_confirmation = True
                current.status_reason = _PAUSE_MESSAGES[fresh_reason]
                self._record(
                    current,
                    "goal.auto_paused",
                    actor,
                    {
                        "reason": fresh_reason,
                        "auto_turns_used": current.auto_turns_used,
                        "tokens_used": current.tokens_used,
                        "wall_elapsed_seconds": _fresh_elapsed,
                    },
                )
                return current

            goal = self._store.mutate(goal_id, mutate)
            reason = goal.paused_reason or reason
            wall_elapsed = self._wall_elapsed_seconds(goal, decided_at) or 0
        return self._build_continue_decision(
            goal,
            reason=reason,
            wall_elapsed_seconds=wall_elapsed,
            decided_at=decided_at,
        )

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
            if actor == "user":
                if any(kind != "user-confirmation" for kind, _ in clean_evidence):
                    raise ValueError(
                        "user completion requires explicit user-confirmation evidence"
                    )
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
            else:
                verified = [
                    item
                    for item in goal.completion_evidence
                    if item.kind == _VERIFIED_EVIDENCE_KIND
                ]
                verified_references = {item.reference for item in verified}
                requested_references = {reference for _, reference in clean_evidence}
                if _LATEST_VERIFICATION_REFERENCE in requested_references:
                    if not verified:
                        raise ValueError(
                            "latest-verification is unavailable for this goal"
                        )
                    requested_references.remove(_LATEST_VERIFICATION_REFERENCE)
                    requested_references.add(verified[-1].reference)
                missing = requested_references - verified_references
                if missing:
                    raise ValueError(
                        "agent completion requires daemon-verified evidence references: "
                        + ", ".join(sorted(missing))
                    )
                covered = {
                    criterion
                    for item in verified
                    if item.reference in requested_references
                    for criterion in item.covered_criteria
                }
                unmet = [
                    criterion
                    for criterion in goal.completion_criteria
                    if criterion not in covered
                ]
                if unmet:
                    raise ValueError(
                        "agent completion has unmet completion criteria: "
                        + ", ".join(unmet)
                    )
            goal.status = "completed"
            goal.status_reason = ""
            goal.paused_reason = ""
            goal.paused_needs_confirmation = False
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

    # 把 daemon 验证通过事件原子登记为可供模型引用的完成证据
    def record_verification(
        self,
        goal_id: str,
        *,
        run_id: str,
        step: int,
        tool: str,
        action: str,
        summary: str = "",
        covered_criteria: list[str] | None = None,
    ) -> GoalRecord:
        if step < 0:
            raise ValueError("verification step must be non-negative")
        clean_run_id = run_id.strip()
        if not clean_run_id:
            raise ValueError("verification run_id must not be empty")
        reference = (
            f"run://{clean_run_id}/verification/{step}/"
            f"{quote(tool.strip(), safe='')}/{quote(action.strip(), safe='')}"
        )

        # 只接受属于该 Goal 的 run，并按稳定引用幂等落盘
        def mutate(goal: GoalRecord) -> GoalRecord:
            if clean_run_id not in goal.linked_run_ids:
                raise ValueError(f"verification run is not linked to goal: {clean_run_id}")
            if any(item.reference == reference for item in goal.completion_evidence):
                return goal
            declared = set(goal.completion_criteria)
            clean_criteria = list(
                dict.fromkeys(
                    item.strip() for item in covered_criteria or [] if item.strip()
                )
            )
            unknown = [item for item in clean_criteria if item not in declared]
            if unknown:
                raise ValueError(
                    "verification references unknown completion criteria: "
                    + ", ".join(unknown)
                )
            timeline_criteria: list[JsonValue] = list(clean_criteria)
            goal.completion_evidence.append(
                CompletionEvidence(
                    kind=_VERIFIED_EVIDENCE_KIND,
                    reference=reference,
                    summary=summary.strip(),
                    covered_criteria=clean_criteria,
                    recorded_at=_now(),
                )
            )
            self._record(
                goal,
                "goal.verification_recorded",
                "system",
                {
                    "run_id": clean_run_id,
                    "step": step,
                    "tool": tool.strip(),
                    "action": action.strip(),
                    "reference": reference,
                    "covered_criteria": timeline_criteria,
                },
            )
            return goal

        return self._store.mutate(goal_id, mutate)

    # 在模型调用前原子预留当前全部可用 token 并返回稳定 lease ID 与额度
    def reserve_token_lease(
        self,
        goal_id: str,
        *,
        lease_id: str,
        requested_tokens: int | None = None,
        minimum_tokens: int = 1,
        actor: str = "system",
    ) -> tuple[str, int]:
        clean_lease_id = lease_id.strip()
        if not clean_lease_id:
            raise ValueError("goal token lease_id must not be empty")
        if requested_tokens is not None and requested_tokens < 1:
            raise ValueError("goal requested token lease must be positive")
        if minimum_tokens < 1:
            raise ValueError("goal minimum token lease must be positive")
        if requested_tokens is not None and minimum_tokens > requested_tokens:
            raise ValueError("goal minimum token lease exceeds requested tokens")

        # 在 GoalStore 单一写锁内读取余额和登记 lease，禁止父子并发超额预留
        def mutate(goal: GoalRecord) -> GoalRecord:
            if goal.token_budget is None:
                raise ValueError("cannot reserve tokens for an unbudgeted goal")
            if goal.status != "active":
                raise ValueError(f"cannot reserve tokens for {goal.status} goal")
            if clean_lease_id in goal.token_reservations:
                raise ValueError(f"goal token lease already exists: {clean_lease_id}")
            available = (
                goal.token_budget - goal.tokens_used - _reserved_tokens(goal)
            )
            if available < minimum_tokens:
                if not goal.token_reservations and goal.status == "active":
                    goal.status = "paused"
                    goal.status_reason = "token budget cannot cover the next request"
                    goal.paused_reason = "token_budget_exhausted"
                    goal.paused_needs_confirmation = True
                    self._record(
                        goal,
                        "goal.budget_exhausted",
                        actor,
                        {
                            "available_tokens": max(0, available),
                            "minimum_tokens": minimum_tokens,
                            "tokens_used": goal.tokens_used,
                        },
                    )
                return goal
            reserved = (
                available
                if requested_tokens is None
                else min(available, requested_tokens)
            )
            goal.token_reservations[clean_lease_id] = reserved
            self._record(
                goal,
                "goal.tokens_reserved",
                actor,
                {
                    "lease_id": clean_lease_id,
                    "reserved_tokens": reserved,
                    "requested_tokens": requested_tokens,
                    "minimum_tokens": minimum_tokens,
                    "tokens_used": goal.tokens_used,
                },
            )
            return goal

        updated = self._store.mutate(goal_id, mutate)
        reserved = updated.token_reservations.get(clean_lease_id)
        if reserved is None:
            if updated.token_reservations and updated.status == "active":
                raise ValueError("goal token budget is temporarily reserved")
            raise ValueError("goal token budget is fully reserved or exhausted")
        return clean_lease_id, reserved

    # 用真实 usage 结算 token lease，usage 缺失时保守消耗全部预留以保持硬上限
    def settle_token_lease(
        self,
        goal_id: str,
        *,
        lease_id: str,
        actual_tokens: int | None,
        actor: str = "system",
    ) -> GoalRecord:
        if actual_tokens is not None and actual_tokens < 0:
            raise ValueError("goal token usage must not be negative")

        # 结算和释放在同一存储事务完成，任何观察者都不会看到重复可用余额
        def mutate(goal: GoalRecord) -> GoalRecord:
            try:
                reserved = goal.token_reservations.pop(lease_id)
            except KeyError as exc:
                raise ValueError(f"unknown goal token lease: {lease_id}") from exc
            charged = reserved if actual_tokens is None else actual_tokens
            overflow = charged > reserved
            goal.tokens_used += charged
            exhausted = (
                goal.token_budget is not None
                and goal.tokens_used >= goal.token_budget
            )
            if (exhausted or overflow or actual_tokens is None) and goal.status == "active":
                goal.status = "paused"
                if actual_tokens is None:
                    goal.status_reason = _PAUSE_MESSAGES["token_usage_unavailable"]
                    goal.paused_reason = "token_usage_unavailable"
                else:
                    goal.status_reason = _PAUSE_MESSAGES["token_budget_exhausted"]
                    goal.paused_reason = "token_budget_exhausted"
                goal.paused_needs_confirmation = True
            self._record(
                goal,
                "goal.tokens_settled",
                actor,
                {
                    "lease_id": lease_id,
                    "reserved_tokens": reserved,
                    "actual_tokens": actual_tokens,
                    "charged_tokens": charged,
                    "usage_unavailable": actual_tokens is None,
                    "reservation_overflow": overflow,
                    "tokens_used": goal.tokens_used,
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
                goal.paused_reason = "token_budget_exhausted"
                goal.paused_needs_confirmation = True
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
            covered = _covered_criteria(goal)
            lines.append(
                "Verified criteria: "
                + (", ".join(item for item in goal.completion_criteria if item in covered)
                   or "none")
            )
            lines.append(
                "Unmet criteria: "
                + ", ".join(
                    item for item in goal.completion_criteria if item not in covered
                )
            )
        if goal.token_budget is not None:
            lines.append(
                f"Token budget: {goal.tokens_used}/{goal.token_budget} used; "
                f"{_reserved_tokens(goal)} reserved"
            )
        if goal.auto_continue:
            lines.append(
                "Automatic continuation window: "
                f"{goal.auto_turns_used}/{goal.max_auto_turns} turns used; "
                f"{goal.max_wall_seconds}s wall-clock ceiling"
            )
            lines.append(
                "Automatic continuation may only proceed after the daemon confirms "
                "completion evidence, token budget, turn budget, wall-clock budget, "
                "and the frozen permission ceiling."
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
            if status == "paused":
                goal.paused_reason = reason.strip() or "paused_by_user"
                goal.paused_needs_confirmation = False
            else:
                goal.paused_reason = ""
                goal.paused_needs_confirmation = False
            self._record(
                goal,
                "goal.status_changed",
                actor,
                {"status": status, "reason": goal.status_reason},
            )
            return goal

        return self._store.mutate(goal_id, mutate)

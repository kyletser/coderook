from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from code_rook.core.authority.models import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    ToolAction,
    WorkspaceTrust,
)


class AuthorityDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class AuthorityEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AuthorityDecision
    reason: str


_PROFILE_RANK = {
    AuthorityProfile.ASK: 0,
    AuthorityProfile.AUTO_REVIEW: 1,
    AuthorityProfile.FULL_ACCESS: 2,
}


# 按 mode、权限姿态和任务范围评估单个工具 action
def evaluate_action(
    snapshot: AuthoritySnapshot,
    action: ToolAction | str,
) -> AuthorityEvaluation:
    try:
        known_action = ToolAction(action)
    except ValueError:
        return AuthorityEvaluation(
            decision=AuthorityDecision.DENY,
            reason=f"unknown capability: {action}",
        )
    if known_action not in snapshot.allowed_actions:
        return AuthorityEvaluation(
            decision=AuthorityDecision.DENY,
            reason=f"action outside authority scope: {known_action.value}",
        )
    if snapshot.mode == RuntimeMode.PLAN and known_action != ToolAction.READ:
        return AuthorityEvaluation(
            decision=AuthorityDecision.DENY,
            reason=f"plan mode denies {known_action.value}",
        )
    if known_action == ToolAction.READ:
        return AuthorityEvaluation(
            decision=AuthorityDecision.ALLOW,
            reason="read action is within authority scope",
        )
    if snapshot.profile == AuthorityProfile.FULL_ACCESS:
        return AuthorityEvaluation(
            decision=AuthorityDecision.ALLOW,
            reason="full access permits the action subject to hard policy",
        )
    if (
        snapshot.profile == AuthorityProfile.AUTO_REVIEW
        and known_action == ToolAction.MUTATE
    ):
        return AuthorityEvaluation(
            decision=AuthorityDecision.ALLOW,
            reason="auto review permits workspace mutation subject to hard policy",
        )
    return AuthorityEvaluation(
        decision=AuthorityDecision.ASK,
        reason=f"{snapshot.profile.value} requires approval for {known_action.value}",
    )


# 取父权限、profile ceiling 和 task scope 的交集生成 child 快照
def narrow_child_authority(
    parent: AuthoritySnapshot,
    *,
    profile_ceiling: AuthorityProfile,
    allowed_actions: frozenset[ToolAction],
    requested_mode: RuntimeMode | None = None,
    requested_trust: WorkspaceTrust | None = None,
) -> AuthoritySnapshot:
    profile = min(
        (parent.profile, profile_ceiling),
        key=_PROFILE_RANK.__getitem__,
    )
    mode = requested_mode or parent.mode
    if parent.mode == RuntimeMode.PLAN:
        mode = RuntimeMode.PLAN
    trust = requested_trust or parent.workspace_trust
    if parent.workspace_trust == WorkspaceTrust.UNTRUSTED:
        trust = WorkspaceTrust.UNTRUSTED
    return AuthoritySnapshot(
        mode=mode,
        profile=profile,
        workspace_trust=trust,
        sandbox=parent.sandbox,
        allowed_actions=parent.allowed_actions & allowed_actions,
    )

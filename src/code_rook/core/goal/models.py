from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from code_rook.core.authority import AuthoritySnapshot

GoalStatus = Literal["active", "paused", "blocked", "completed", "cleared"]
ReservedTokenCount = Annotated[int, Field(gt=0)]
GOAL_SCHEMA_VERSION: Literal[4] = 4


class UnsupportedGoalSchemaError(ValueError):
    pass


class CompletionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    summary: str = ""
    covered_criteria: list[str] = Field(default_factory=list)
    recorded_at: str


class GoalTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    event: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    at: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class GoalContinueDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    goal_id: str
    session_id: str
    should_continue: bool
    reason: str
    auto_turns_used: int = Field(ge=0)
    remaining_auto_turns: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    token_budget: int | None = Field(default=None, ge=1)
    remaining_tokens: int | None = Field(default=None, ge=0)
    wall_elapsed_seconds: int = Field(ge=0)
    max_wall_seconds: int = Field(ge=1)
    permission_ceiling: AuthoritySnapshot
    paused_needs_confirmation: bool = False
    decided_at: str


class GoalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[4] = GOAL_SCHEMA_VERSION
    id: str = Field(min_length=1, pattern=r"^goal-[a-f0-9]{12}$")
    session_id: str = Field(default="legacy", min_length=1)
    objective: str = Field(min_length=1)
    status: GoalStatus = "active"
    status_reason: str = ""
    token_budget: int | None = Field(default=None, ge=1)
    tokens_used: int = Field(default=0, ge=0)
    token_reservations: dict[str, ReservedTokenCount] = Field(default_factory=dict)
    elapsed_ms: int = Field(default=0, ge=0)
    auto_continue: bool = False
    max_auto_turns: int = Field(default=3, ge=1, le=100)
    auto_turns_used: int = Field(default=0, ge=0)
    max_wall_seconds: int = Field(default=1800, ge=1, le=86_400)
    auto_window_started_at: str | None = None
    permission_ceiling: AuthoritySnapshot = Field(default_factory=AuthoritySnapshot)
    paused_reason: str = ""
    paused_needs_confirmation: bool = False
    constraints: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    linked_task_ids: list[int] = Field(default_factory=list)
    linked_run_ids: list[str] = Field(default_factory=list)
    current_run_id: str | None = None
    completion_evidence: list[CompletionEvidence] = Field(default_factory=list)
    timeline: list[GoalTimelineEntry] = Field(default_factory=list)
    created_at: str
    updated_at: str

    # 从旧版 Goal 字典补齐当前默认字段，同时拒绝未来 schema 被旧代码降级
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GoalRecord:
        payload = dict(data)
        raw_version = payload.get("schema_version", 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ValueError("invalid goal schema version")
        if raw_version > GOAL_SCHEMA_VERSION:
            raise UnsupportedGoalSchemaError(
                f"goal schema {raw_version} is newer than supported {GOAL_SCHEMA_VERSION}"
            )
        if raw_version < 1:
            raise ValueError(f"invalid goal schema version: {raw_version}")
        payload["schema_version"] = GOAL_SCHEMA_VERSION
        return cls.model_validate(payload)

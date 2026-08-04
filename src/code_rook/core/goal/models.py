from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

GoalStatus = Literal["active", "blocked", "completed"]


class CompletionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    summary: str = ""
    recorded_at: str


class GoalTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int = Field(ge=1)
    event: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    at: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class GoalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = 1
    id: str = Field(min_length=1, pattern=r"^goal-[a-f0-9]{12}$")
    objective: str = Field(min_length=1)
    status: GoalStatus = "active"
    token_budget: int | None = Field(default=None, ge=1)
    tokens_used: int = Field(default=0, ge=0)
    elapsed_ms: int = Field(default=0, ge=0)
    constraints: list[str] = Field(default_factory=list)
    linked_task_ids: list[int] = Field(default_factory=list)
    completion_evidence: list[CompletionEvidence] = Field(default_factory=list)
    timeline: list[GoalTimelineEntry] = Field(default_factory=list)
    created_at: str
    updated_at: str

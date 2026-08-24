from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from code_rook.core.authority import AuthoritySnapshot


class WorkerStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    BUDGET_LIMITED = "budget_limited"


ACTIVE_WORKER_STATUSES = frozenset(
    {WorkerStatus.QUEUED, WorkerStatus.RUNNING, WorkerStatus.WAITING}
)


class WriteClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    read_only: bool = False
    exact_files: list[str] = Field(default_factory=list)
    write_roots: list[str] = Field(default_factory=list)
    coordination_contract: str = ""

    @model_validator(mode="after")
    # 校验写入型 worker 至少声明一种可协调的写权限范围
    def validate_scope(self) -> WriteClaim:
        if not self.read_only and not (
            self.exact_files or self.write_roots or self.coordination_contract.strip()
        ):
            raise ValueError(
                "write worker requires exact_files, write_roots, or coordination_contract"
            )
        return self


class WorkerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: int = 6
    id: str = Field(min_length=1)
    parent_turn_id: str = Field(min_length=1)
    parent_worker_id: str = ""
    root_goal_id: str = Field(min_length=1)
    session_id: str = ""
    description: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    role: str = "general-purpose"
    profile: str = ""
    profile_digest: str = Field(default="", pattern=r"^(?:|sha256:[a-f0-9]{64})$")
    route: str = ""
    route_digest: str = Field(default="", pattern=r"^(?:|[a-f0-9]{64})$")
    model: str = ""
    reasoning: str = ""
    backend: str = "builtin"
    backend_capabilities: dict[str, JsonValue] = Field(default_factory=dict)
    sandbox_enforcement: Literal["full", "partial", "unavailable"] = "unavailable"
    status: WorkerStatus = WorkerStatus.QUEUED
    status_reason: str = ""
    depth: int = Field(default=1, ge=1, le=8)
    max_steps: int = Field(default=20, ge=1)
    wall_time_s: int = Field(default=900, ge=1)
    workspace: str = Field(min_length=1)
    worktree: str = ""
    branch: str = ""
    base_commit: str = ""
    merge_owner: str = ""
    merge_reviewer: str = ""
    authority_ceiling: AuthoritySnapshot = Field(default_factory=AuthoritySnapshot)
    write_claim: WriteClaim = Field(default_factory=lambda: WriteClaim(read_only=True))
    dependencies: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    heartbeat_at: str
    heartbeat_interval_s: float = Field(default=10.0, gt=0)
    lease_timeout_s: float = Field(default=30.0, gt=0)
    boot_id: str = Field(min_length=1)
    token_budget: int | None = Field(default=None, ge=1)
    token_usage: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    cost_status: str = "unpriced"
    attempt: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_s: float = Field(default=1.0, ge=0)
    retry_after: str = ""
    summary: str = ""
    changes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    event_cursor: int = Field(default=0, ge=0)
    artifact_handles: list[str] = Field(default_factory=list)
    handoff_status: str = "read_only"
    changed_files: list[str] = Field(default_factory=list)
    diff_stat: str = ""
    diff_preview: str = ""
    diff_truncated: bool = False
    verification_status: str = "not_reported"
    approved: bool | None = None
    review_digest: str = ""
    receipt: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    started_at: str = ""
    ended_at: str = ""

    @model_validator(mode="after")
    # 校验租约、重试和独立 worktree 合并责任人约束
    def validate_worker_contract(self) -> WorkerRecord:
        if self.heartbeat_interval_s >= self.lease_timeout_s:
            raise ValueError("heartbeat interval must be less than lease timeout")
        if self.attempt > self.max_attempts:
            raise ValueError("worker attempt exceeds max attempts")
        if self.worktree and not self.write_claim.read_only and not (
            self.merge_owner.strip() and self.merge_reviewer.strip()
        ):
            raise ValueError("writing worktree worker requires merge owner and reviewer")
        return self


class WorkerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cursor: int = Field(ge=1)
    worker_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    summary: str = ""
    at: str

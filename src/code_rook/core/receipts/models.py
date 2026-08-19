from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from code_rook.core.authority import (
    AuthorityProfile,
    RuntimeMode,
    SandboxCapability,
    ToolAction,
    WorkspaceTrust,
)
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.runtime.models import TurnStatus


class SandboxPlanReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str
    tier: Literal["read_only", "workspace_write", "none"]
    workspace: str
    network: bool
    allowed_domains: list[str]
    domain_policy_enforced: bool
    writable_roots: list[str]
    enforced: bool
    degraded_reason: str
    policy_version: int = Field(ge=1)


class TurnAuthorityReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RuntimeMode
    profile: AuthorityProfile
    workspace_trust: WorkspaceTrust
    sandbox: SandboxCapability
    sandbox_plan: SandboxPlanReceipt
    allowed_actions: frozenset[ToolAction]


class TurnApprovalCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: int = Field(ge=0)
    granted: int = Field(ge=0)
    denied: int = Field(ge=0)


class TurnProcessUsageReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_count: int = Field(ge=0)
    complete_records: int = Field(ge=0)
    total_process_wall_ms: int = Field(ge=0)
    user_cpu_ms: int = Field(ge=0)
    system_cpu_ms: int = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    process_count: int = Field(ge=0)


class TurnReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str
    thread_id: str
    route: RouteReceipt | None
    authority: TurnAuthorityReceipt
    started_at: datetime
    finished_at: datetime | None
    status: TurnStatus
    usage: dict[str, JsonValue]
    cost: JsonValue = "unknown"
    tool_call_count: int = Field(ge=0)
    approvals: TurnApprovalCounts
    process_usage: TurnProcessUsageReceipt
    files_changed: list[str]
    checkpoints: list[dict[str, JsonValue]]
    artifacts: list[dict[str, JsonValue]]
    workers: list[dict[str, JsonValue]]
    verification: list[dict[str, JsonValue]]
    error_classification: str | None
    unavailable: list[
        Literal[
            "route",
            "usage",
            "cost",
            "files_changed",
            "checkpoints",
            "artifacts",
            "workers",
            "verification",
            "error_classification",
        ]
    ]

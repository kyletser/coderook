from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.subagent.models import WriteClaim

_NODE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


class WorkflowLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_nodes: int = Field(default=128, ge=1, le=1_024)
    max_depth: int = Field(default=8, ge=1, le=32)
    max_concurrency: int = Field(default=4, ge=1, le=32)
    token_budget: int | None = Field(default=None, ge=1)
    wall_time_s: int = Field(default=3_600, ge=1, le=86_400)


class WorkerStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["worker"] = "worker"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    description: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=32_000)
    profile: str = ""
    route: str = ""
    model: str = ""
    reasoning: str = ""
    authority_ceiling: AuthoritySnapshot = Field(default_factory=AuthoritySnapshot)
    write_claim: WriteClaim = Field(default_factory=lambda: WriteClaim(read_only=True))
    high_risk_write: bool = False
    acceptance: list[str] = Field(default_factory=list, max_length=50)
    token_budget: int | None = Field(default=None, ge=1)
    wall_time_s: int = Field(default=900, ge=1, le=86_400)

    @model_validator(mode="after")
    # 高风险写节点必须具有真实写声明，不能伪装成只读步骤
    def validate_high_risk_write(self) -> WorkerStep:
        if self.high_risk_write and self.write_claim.read_only:
            raise ValueError("high-risk worker must declare a write claim")
        return self


class SequenceStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["sequence"] = "sequence"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    steps: list[WorkflowStep] = Field(min_length=1)


class ParallelStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["parallel"] = "parallel"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    steps: list[WorkflowStep] = Field(min_length=1)
    max_concurrency: int | None = Field(default=None, ge=1, le=32)


class BranchCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(pattern=_NODE_ID_PATTERN)
    field: Literal["status", "summary", "approved"] = "status"
    operator: Literal["eq", "ne", "contains"] = "eq"
    value: JsonValue


class BranchStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["branch"] = "branch"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    condition: BranchCondition
    then_step: WorkflowStep
    else_step: WorkflowStep


class RetryStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["retry"] = "retry"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    step: WorkflowStep
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_s: float = Field(default=1.0, ge=0, le=300)


class ReviewGateStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["review_gate"] = "review_gate"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    step: WorkflowStep
    reviewer: WorkerStep

    @model_validator(mode="after")
    # reviewer 必须保持只读，避免审查 gate 自身修改被审对象
    def validate_reviewer(self) -> ReviewGateStep:
        if not self.reviewer.write_claim.read_only:
            raise ValueError("review gate reviewer must be read-only")
        return self


class FanInStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["fan_in"] = "fan_in"
    id: str = Field(pattern=_NODE_ID_PATTERN)
    steps: list[WorkflowStep] = Field(min_length=1)
    owner: str = Field(min_length=1, max_length=100)
    reducer: Literal["collect_evidence"] = "collect_evidence"


WorkflowStep = Annotated[
    WorkerStep
    | SequenceStep
    | ParallelStep
    | BranchStep
    | RetryStep
    | ReviewGateStep
    | FanInStep,
    Field(discriminator="type"),
]


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    id: str = Field(pattern=_NODE_ID_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    inputs: dict[str, JsonValue] = Field(default_factory=dict)
    root: WorkflowStep
    limits: WorkflowLimits = Field(default_factory=WorkflowLimits)


for _model in (
    SequenceStep,
    ParallelStep,
    BranchStep,
    RetryStep,
    ReviewGateStep,
    FanInStep,
    WorkflowSpec,
):
    _model.model_rebuild()

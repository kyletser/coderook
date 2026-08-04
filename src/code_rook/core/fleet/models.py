from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.workflow import WorkerStep

_PROFILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"


class FleetProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_PROFILE_PATTERN)
    route: str = ""
    model: str = ""
    reasoning: str = ""
    authority_ceiling: AuthoritySnapshot = Field(default_factory=AuthoritySnapshot)


class LocalWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    workflow_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    workspace: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    step: WorkerStep

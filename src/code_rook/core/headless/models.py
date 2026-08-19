from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HeadlessRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    thread_id: str = ""
    status: Literal["success", "failed", "interrupted"]
    reason: str | None = None
    exit_code: int = Field(ge=0)
    result: str = ""
    steps: int = Field(default=0, ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)


class HeadlessEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    kind: Literal["event", "result"]
    sequence: int = Field(ge=1)
    run_id: str
    type: str
    payload: dict[str, Any]

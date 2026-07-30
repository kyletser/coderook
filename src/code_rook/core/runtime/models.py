from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class ThreadStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    ARCHIVED = "archived"


class TurnStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class RuntimeMode(StrEnum):
    PLAN = "plan"
    ACT = "act"
    OPERATE = "operate"


class TurnItemKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    CHECKPOINT = "checkpoint"


class ThreadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    title: str
    workspace: str = Field(min_length=1)
    status: ThreadStatus = ThreadStatus.IDLE
    default_route_id: str | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: int = Field(default=1, ge=1)


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: TurnStatus = TurnStatus.QUEUED
    mode: RuntimeMode = RuntimeMode.ACT
    authority_profile: str = Field(default="ask", min_length=1)
    route: dict[str, JsonValue] | None = None
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    error: dict[str, JsonValue] | None = None
    boot_id: str | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: int = Field(default=1, ge=1)


class TurnItemRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    kind: TurnItemKind
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    tool_call_id: str | None = None
    created_at: datetime
    schema_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    # 校验工具调用与工具结果都携带稳定的调用标识
    def _validate_tool_call_id(self) -> TurnItemRecord:
        needs_id = self.kind in {TurnItemKind.TOOL_CALL, TurnItemKind.TOOL_RESULT}
        if needs_id and not self.tool_call_id:
            raise ValueError(f"{self.kind.value} requires tool_call_id")
        if not needs_id and self.tool_call_id is not None:
            raise ValueError(f"{self.kind.value} does not accept tool_call_id")
        return self


class RuntimeEventRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1)
    turn_id: str | None = None
    seq: int = Field(ge=1)
    type: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    ts: datetime
    schema_version: int = Field(default=1, ge=1)


class SessionFacadeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1)
    mode: Literal["one_shot", "chat"]
    parent_thread_id: str | None = None

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from code_rook.core.authority.models import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    SandboxCapability,
    ToolAction,
    WorkspaceTrust,
)
from code_rook.core.llm.routes import RouteReceipt

RUNTIME_RECORD_SCHEMA_VERSION = 1


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


class TurnItemKind(StrEnum):
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    CHECKPOINT = "checkpoint"


# 返回 turn 默认可进入 authority 评估的全部已知 action
def _all_tool_actions() -> frozenset[ToolAction]:
    return frozenset(ToolAction)


class ThreadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    title: str
    workspace: str = Field(min_length=1)
    status: ThreadStatus = ThreadStatus.IDLE
    default_route_id: str | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: Literal[1] = 1


class TurnRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: TurnStatus = TurnStatus.QUEUED
    mode: RuntimeMode = RuntimeMode.ACT
    authority_profile: AuthorityProfile = AuthorityProfile.ASK
    workspace_trust: WorkspaceTrust = WorkspaceTrust.UNTRUSTED
    sandbox: SandboxCapability = SandboxCapability(
        available=False,
        kind="none",
        reason="sandbox capability has not been detected",
    )
    allowed_actions: frozenset[ToolAction] = Field(
        default_factory=_all_tool_actions
    )
    route: RouteReceipt | None = None
    usage: dict[str, JsonValue] = Field(default_factory=dict)
    error: dict[str, JsonValue] | None = None
    boot_id: str | None = None
    created_at: datetime
    updated_at: datetime
    schema_version: Literal[1] = 1

    @property
    # 返回 turn 启动时冻结的有效 authority 快照
    def authority_snapshot(self) -> AuthoritySnapshot:
        return AuthoritySnapshot(
            mode=self.mode,
            profile=self.authority_profile,
            workspace_trust=self.workspace_trust,
            sandbox=self.sandbox,
            allowed_actions=self.allowed_actions,
        )


class TurnItemRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    kind: TurnItemKind
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    tool_call_id: str | None = None
    created_at: datetime
    schema_version: Literal[1] = 1

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
    schema_version: Literal[1] = 1


class SessionFacadeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1)
    mode: Literal["one_shot", "chat"]
    parent_thread_id: str | None = None
    schema_version: Literal[1] = 1

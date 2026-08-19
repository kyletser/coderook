from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

HookEvent = Literal[
    "session_start",
    "message_submit",
    "turn_start",
    "tool_call_before",
    "tool_call_after",
    "approval_requested",
    "compaction_completed",
    "worker_started",
    "worker_finished",
    "turn_stop",
    "session_stop",
]
HookTrustedScope = Literal["builtin", "user", "project"]
HookFailurePolicy = Literal["open", "closed"]
HookAuditStatus = Literal[
    "completed",
    "blocked",
    "failed",
    "timeout",
    "dropped",
    "skipped_untrusted",
]


class HookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    event: HookEvent
    timeout_ms: int = Field(ge=1, le=300_000)
    blocking: bool
    command: tuple[str, ...] = Field(min_length=1)
    conditions: dict[str, str]
    trusted_scope: HookTrustedScope
    on_failure: HookFailurePolicy = "open"
    max_output_bytes: int = Field(default=65_536, ge=1, le=1_048_576)

    @model_validator(mode="after")
    # 拒绝空命令参数，避免配置成功后在运行期产生含糊执行错误
    def _validate_command(self) -> HookConfig:
        if any(not item.strip() for item in self.command):
            raise ValueError("hook command arguments must not be empty")
        return self


class HookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 2
    event: HookEvent
    hook_id: str
    emitted_at: str
    context: dict[str, JsonValue]
    truncated: bool = False
    original_bytes: int = Field(default=0, ge=0)


class HookAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 2
    hook_id: str
    run_id: str = ""
    event: HookEvent
    status: HookAuditStatus
    blocking: bool
    on_failure: HookFailurePolicy
    elapsed_ms: int = Field(ge=0)
    blocked: bool = False
    reason: str = ""
    output_truncated: bool = False
    exit_code: int | None = None
    process_usage: dict[str, JsonValue] = Field(default_factory=dict)
    ts: str

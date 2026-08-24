from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class SandboxEnforcement(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ExecutionFailureCategory(StrEnum):
    SANDBOX_DENIED = "sandbox_denied"
    SANDBOX_RUNNER_FAILED = "sandbox_runner_failed"
    COMMAND_FAILED = "command_failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class SessionEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=2, ge=2, le=2)
    kind: str = "event"
    session_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    timestamp: str = Field(min_length=1)
    type: str = Field(min_length=1)
    turn_id: str = ""
    step_id: str = ""
    source_event_seqs: tuple[int, ...] = ()
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    provenance: str = "native"
    replay_fidelity: str = "full"

    @model_validator(mode="after")
    # 校验来源序号只能引用当前事件之前的事实且不得重复
    def validate_sources(self) -> SessionEventEnvelope:
        if len(self.source_event_seqs) != len(set(self.source_event_seqs)):
            raise ValueError("source_event_seqs must be unique")
        if any(value < 1 or value >= self.seq for value in self.source_event_seqs):
            raise ValueError("source events must precede the current event")
        return self


class RequestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1, le=1)
    messages: tuple[dict[str, JsonValue], ...]
    system: str
    tool_schemas: tuple[dict[str, JsonValue], ...]
    route_id: str = ""
    model: str = ""
    wire_format: str = ""
    execution_contract_digest: str = ""
    thinking: str = ""
    supports_parallel_tools: bool = False
    supports_images: bool = False
    token_budget: int | None = Field(default=None, ge=1)
    cost_budget_usd: float | None = Field(default=None, ge=0)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    # 从实际 Provider 入参和冻结元数据创建不可变请求快照
    @classmethod
    def create(
        cls,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tool_schemas: list[dict[str, object]],
        metadata: dict[str, object] | None = None,
    ) -> RequestSnapshot:
        raw_metadata = dict(metadata or {})
        payload: dict[str, Any] = {
            "schema_version": 1,
            "messages": messages,
            "system": system,
            "tool_schemas": tool_schemas,
            "route_id": str(raw_metadata.get("route_id", "")),
            "model": str(raw_metadata.get("model", "")),
            "wire_format": str(raw_metadata.get("wire_format", "")),
            "execution_contract_digest": str(
                raw_metadata.get("execution_contract_digest", "")
            ),
            "thinking": str(raw_metadata.get("thinking", "") or ""),
            "supports_parallel_tools": bool(
                raw_metadata.get("supports_parallel_tools", False)
            ),
            "supports_images": bool(raw_metadata.get("supports_images", False)),
            "token_budget": raw_metadata.get("token_budget"),
            "cost_budget_usd": raw_metadata.get("cost_budget_usd"),
        }
        digest = hashlib.sha256(_canonical_json(payload)).hexdigest()
        return cls.model_validate({**payload, "digest": digest})

    # 重新计算快照摘要，供持久化回放验证内容未发生漂移
    def calculated_digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.model_dump(exclude={"digest"}))
        ).hexdigest()


# 将嵌套请求对象编码为确定性的 UTF-8 JSON 字节
def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue

TaskStatus = Literal[
    "pending",
    "ready",
    "running",
    "blocked",
    "completed",
    "failed",
    "cancelled",
]
AttemptStatus = Literal["running", "completed", "failed", "cancelled"]
GateStatus = Literal["pending", "passed", "failed"]
TASK_SCHEMA_VERSION: Literal[2] = 2


class UnsupportedTaskSchemaError(ValueError):
    pass


class TaskAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    owner_worker: str
    status: AttemptStatus = "running"
    started_at: str
    ended_at: str | None = None
    error: str | None = None


class TaskGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    status: GateStatus = "pending"
    evidence: str = ""
    updated_at: str


class TaskArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    digest: str = ""
    media_type: str = "application/octet-stream"
    created_at: str


class TaskTimelineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(ge=1)
    seq: int = Field(ge=1)
    event: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    at: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[2] = TASK_SCHEMA_VERSION
    id: int = Field(ge=1)
    subject: str = Field(min_length=1)
    description: str = ""
    status: TaskStatus = "pending"
    dependencies: list[int] = Field(default_factory=list)
    owner_worker: str = ""
    worktree: str = ""
    attempts: list[TaskAttempt] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    gates: list[TaskGate] = Field(default_factory=list)
    artifacts: list[TaskArtifact] = Field(default_factory=list)
    timeline: list[TaskTimelineEntry] = Field(default_factory=list)
    created_by: str = "agent"
    updated_by: str = "agent"
    created_at: str
    updated_at: str

    @property
    # 提供旧工具和 transcript 使用的 blocked_by 只读兼容视图
    def blocked_by(self) -> list[int]:
        return list(self.dependencies)

    @property
    # 提供旧工具使用的 owner 字段兼容视图
    def owner(self) -> str:
        return self.owner_worker

    # 序列化为版本化 JSON，并保留旧客户端读取的兼容字段
    def to_dict(self) -> dict[str, JsonValue]:
        payload = self.model_dump(mode="json")
        payload["blocked_by"] = list(self.dependencies)
        payload["owner"] = self.owner_worker
        return cast(dict[str, JsonValue], payload)

    @classmethod
    # 从 V1/V2 JSON 构造 TaskRecord，并迁移旧状态和字段名
    def from_dict(cls, data: dict[str, object]) -> TaskRecord:
        payload = dict(data)
        raw_version = payload.get("schema_version", 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ValueError("invalid task schema version")
        if raw_version > TASK_SCHEMA_VERSION:
            raise UnsupportedTaskSchemaError(
                f"task schema {raw_version} is newer than supported {TASK_SCHEMA_VERSION}"
            )
        if raw_version < 1:
            raise ValueError(f"invalid task schema version: {raw_version}")
        payload["schema_version"] = TASK_SCHEMA_VERSION
        task_id = int(str(payload.get("id", "0")))
        payload.pop("blocked_by", None)
        payload.pop("owner", None)
        if "dependencies" not in payload:
            legacy_dependencies = data.get("blocked_by", [])
            if not isinstance(legacy_dependencies, list):
                raise ValueError("task blocked_by must be a list")
            payload["dependencies"] = legacy_dependencies
        if "owner_worker" not in payload:
            payload["owner_worker"] = str(data.get("owner", ""))
        legacy_status = str(payload.get("status", "pending"))
        payload["status"] = {
            "in_progress": "running",
        }.get(legacy_status, legacy_status)
        payload.setdefault("attempts", [])
        payload.setdefault("acceptance_criteria", [])
        payload.setdefault("gates", [])
        payload.setdefault("artifacts", [])
        payload.setdefault("timeline", [])
        raw_timeline = payload["timeline"]
        if not isinstance(raw_timeline, list):
            raise ValueError("task timeline must be a list")
        payload["timeline"] = [
            {"task_id": task_id, **entry}
            if isinstance(entry, dict) and "task_id" not in entry
            else entry
            for entry in raw_timeline
        ]
        payload.setdefault("created_by", "legacy")
        payload.setdefault("updated_by", "legacy")
        return cls.model_validate(payload)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["active", "waiting_for_input", "interrupted", "closed"]
SessionMode = Literal["one_shot", "chat"]
SESSION_SCHEMA_VERSION = 2


@dataclass
class Session:
    id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    run_ids: list[str] = field(default_factory=list)
    parent_session_id: str | None = None
    workspace: str = ""

    # 将 Session 转为可写入 meta.json 的普通 dict
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": self.id,
            "mode": self.mode,
            "status": self.status,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_ids": list(self.run_ids),
            "parent_session_id": self.parent_session_id,
            "workspace": self.workspace,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        schema_version = int(data.get("schema_version", 0))
        if schema_version > SESSION_SCHEMA_VERSION:
            raise ValueError(
                "session schema "
                f"{schema_version} is newer than supported {SESSION_SCHEMA_VERSION}"
            )
        if schema_version < 0:
            raise ValueError(f"invalid session schema version: {schema_version}")
        return cls(
            id=str(data["id"]),
            mode=data["mode"],
            status=data["status"],
            title=str(data.get("title", "")),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            run_ids=[str(x) for x in data.get("run_ids", [])],
            parent_session_id=(
                str(data["parent_session_id"])
                if data.get("parent_session_id") is not None
                else None
            ),
            workspace=str(data.get("workspace", "")),
        )

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from code_rook.core.presets import STANDARD_PRESET, get_agent_preset

SessionStatus = Literal["active", "waiting_for_input", "interrupted", "closed"]
SessionMode = Literal["one_shot", "chat"]
SESSION_SCHEMA_VERSION = 3
SESSION_ID_PATTERN = re.compile(r"^sess-[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class UnsupportedSessionSchemaError(ValueError):
    pass


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
    preset_id: str = STANDARD_PRESET.id
    preset_digest: str = STANDARD_PRESET.digest

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
            "preset_id": self.preset_id,
            "preset_digest": self.preset_digest,
        }

    # 从 meta.json 的 dict 还原 Session 对象
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Session:
        raw_version = data.get("schema_version", 1)
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            raise ValueError("invalid session schema version")
        schema_version = raw_version
        if schema_version > SESSION_SCHEMA_VERSION:
            raise UnsupportedSessionSchemaError(
                "session schema "
                f"{schema_version} is newer than supported {SESSION_SCHEMA_VERSION}"
            )
        if schema_version < 1:
            raise ValueError(f"invalid session schema version: {schema_version}")
        session_id = data.get("id")
        mode = data.get("mode")
        status = data.get("status")
        title = data.get("title", "")
        created_at = data.get("created_at")
        updated_at = data.get("updated_at")
        run_ids = data.get("run_ids", [])
        parent_session_id = data.get("parent_session_id")
        workspace = data.get("workspace", "")
        preset_id = data.get("preset_id", STANDARD_PRESET.id)
        preset_digest = data.get("preset_digest", "")
        if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError("invalid session id")
        if mode not in {"one_shot", "chat"}:
            raise ValueError("invalid session mode")
        if status not in {"active", "waiting_for_input", "interrupted", "closed"}:
            raise ValueError("invalid session status")
        if not isinstance(title, str):
            raise ValueError("invalid session title")
        if not isinstance(created_at, str) or not created_at:
            raise ValueError("invalid session created_at")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError("invalid session updated_at")
        if not isinstance(run_ids, list) or not all(
            isinstance(run_id, str) and run_id for run_id in run_ids
        ):
            raise ValueError("invalid session run_ids")
        if parent_session_id is not None and (
            not isinstance(parent_session_id, str)
            or SESSION_ID_PATTERN.fullmatch(parent_session_id) is None
        ):
            raise ValueError("invalid parent session id")
        if not isinstance(workspace, str):
            raise ValueError("invalid session workspace")
        if not isinstance(preset_id, str) or not preset_id:
            raise ValueError("invalid session preset id")
        try:
            preset = get_agent_preset(preset_id)
        except KeyError:
            if schema_version >= 3:
                raise ValueError("unknown session preset") from None
            preset = STANDARD_PRESET
            preset_id = preset.id
        if not preset_digest:
            preset_digest = preset.digest
        if not isinstance(preset_digest, str) or re.fullmatch(
            r"[0-9a-f]{64}", preset_digest
        ) is None:
            raise ValueError("invalid session preset digest")
        if schema_version >= 3 and preset_digest != preset.digest:
            raise ValueError("session preset digest does not match the installed preset")
        return cls(
            id=session_id,
            mode=mode,
            status=status,
            title=title,
            created_at=created_at,
            updated_at=updated_at,
            run_ids=list(run_ids),
            parent_session_id=parent_session_id,
            workspace=workspace,
            preset_id=preset_id,
            preset_digest=preset_digest,
        )

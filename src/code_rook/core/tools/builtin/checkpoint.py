from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from code_rook.core.checkpoints import CheckpointError, CheckpointStore
from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect


class CheckpointListParams(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CheckpointListTool(BaseTool):
    params_model = CheckpointListParams
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    name = "checkpoint_list"
    description = "List file checkpoints created automatically during the current agent run."
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, store: CheckpointStore) -> None:
        self._store = store

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        CheckpointListParams.model_validate(params)
        checkpoints = [
            {
                "checkpoint_id": item.checkpoint_id,
                "label": item.label,
                "created_at": item.created_at,
                "status": item.status,
                "paths": item.paths,
            }
            for item in self._store.list_checkpoints()
        ]
        return ToolResult(
            json.dumps(
                {"checkpoint_count": len(checkpoints), "checkpoints": checkpoints},
                ensure_ascii=False,
                indent=2,
            )
        )


class CheckpointRewindParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    checkpoint_id: str | None = None


class CheckpointRewindTool(BaseTool):
    params_model = CheckpointRewindParams
    side_effect = ToolSideEffect.LOCAL_WRITE
    name = "checkpoint_rewind"
    description = (
        "Restore files from a checkpoint created in the current run. Defaults to the latest ready "
        "checkpoint and refuses to overwrite files changed after the checkpoint."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "checkpoint_id": {
                "type": "string",
                "description": "Checkpoint id from checkpoint_list; omit for the latest ready one.",
            }
        },
    }

    def __init__(self, store: CheckpointStore) -> None:
        self._store = store

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = CheckpointRewindParams.model_validate(params)
        try:
            outcome = self._store.rewind(request.checkpoint_id)
        except CheckpointError as exc:
            return ToolResult(
                json.dumps(
                    {
                        "error": {
                            "code": exc.code,
                            "message": str(exc),
                            "conflicts": exc.conflicts,
                        }
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                is_error=True,
                error_type=("conflict" if exc.code == "rewind_conflict" else "runtime_error"),
            )
        return ToolResult(
            json.dumps(
                {
                    "checkpoint_id": outcome.checkpoint_id,
                    "restored": outcome.restored,
                    "already_restored": outcome.already_restored,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

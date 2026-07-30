from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.editing import EditEngine, EditError
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_text: str
    new_text: str
    replace_all: bool = False
    expected_hash: str | None = None

    @model_validator(mode="after")
    def _validate_edit(self) -> EditFileParams:
        if not self.old_text:
            raise ValueError("old_text must be non-empty")
        if self.old_text == self.new_text:
            raise ValueError("old_text and new_text must differ")
        return self


class EditFileTool(BaseTool):
    params_model = EditFileParams
    side_effect = ToolSideEffect.LOCAL_WRITE
    name = "edit_file"
    description = (
        "Replace exact text in an existing UTF-8 file inside the workspace. Read the file first "
        "and pass its content_hash as expected_hash for conflict detection. A match must be "
        "unique unless replace_all is true. Writes are atomic and return a bounded unified diff."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path to an existing text file.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to replace; include enough context to make it unique.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text. Use an empty string to delete old_text.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every exact match instead of requiring one unique match.",
            },
            "expected_hash": {
                "type": "string",
                "description": "Optional SHA-256 content_hash returned by read_file.",
            },
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(
        self,
        boundary: WorkspaceBoundary | None = None,
        *,
        workspace_root: Path | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        if boundary is not None and workspace_root is not None:
            raise ValueError("pass either boundary or workspace_root, not both")
        resolved_boundary = boundary or WorkspaceBoundary(workspace_root or Path.cwd())
        self._engine = EditEngine(resolved_boundary, checkpoint_store=checkpoint_store)

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = EditFileParams.model_validate(params)
        try:
            outcome = self._engine.edit(
                request.path,
                request.old_text,
                request.new_text,
                replace_all=request.replace_all,
                expected_hash=request.expected_hash,
            )
        except EditError as exc:
            payload = {"error": {"code": exc.code, "message": str(exc)}}
            conflict_codes = {"hash_mismatch", "concurrent_change"}
            error_type = "conflict" if exc.code in conflict_codes else "runtime_error"
            return ToolResult(
                json.dumps(payload, ensure_ascii=False, indent=2),
                is_error=True,
                error_type=error_type,
            )

        return ToolResult(
            json.dumps(
                {
                    "path": outcome.path,
                    "replacements": outcome.replacements,
                    "old_hash": outcome.old_hash,
                    "new_hash": outcome.new_hash,
                    "bytes_written": outcome.bytes_written,
                    "diff": outcome.diff,
                    "diff_truncated": outcome.diff_truncated,
                    "checkpoint_id": outcome.checkpoint_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

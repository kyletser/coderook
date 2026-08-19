from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.patching import PatchEngine, PatchError
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary


class ApplyPatchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    patch: str
    dry_run: bool = False
    selected_hunks: list[str] | None = None
    expected_plan_id: str | None = None


class ApplyPatchTool(BaseTool):
    params_model = ApplyPatchParams
    side_effect = ToolSideEffect.LOCAL_WRITE
    name = "apply_patch"
    description = (
        "Apply a standard multi-file unified diff inside the workspace. Every file and hunk is "
        "validated before a transactional commit; failures return structured diagnostics and do "
        "not leave partial file changes. Supports UTF-8 file additions, modifications and "
        "deletions."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "Standard unified diff with ---/+++ file headers and @@ hunks.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Validate and summarize the patch without changing files.",
            },
        },
        "required": ["patch"],
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
        self._engine = PatchEngine(
            resolved_boundary,
            checkpoint_store=checkpoint_store,
        )

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = ApplyPatchParams.model_validate(params)
        try:
            if request.selected_hunks is not None:
                plan = self._engine.plan(request.patch)
                if request.expected_plan_id is not None and plan.id != request.expected_plan_id:
                    raise PatchError(
                        "stale_patch_plan",
                        "workspace changed after patch review; generate a new patch plan",
                    )
                if request.dry_run:
                    raise PatchError(
                        "invalid_selection",
                        "selected_hunks cannot be combined with dry_run",
                    )
                outcome = self._engine.apply_plan(plan, set(request.selected_hunks))
            else:
                outcome = self._engine.apply(request.patch, dry_run=request.dry_run)
        except PatchError as exc:
            error_payload = {
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "path": exc.path,
                    "hunk": exc.hunk,
                    "line": exc.line,
                    "expected": exc.expected,
                    "actual": exc.actual,
                }
            }
            error_type = "conflict" if exc.code in {
                "concurrent_change",
                "hunk_mismatch",
                "target_exists",
            } else "runtime_error"
            return ToolResult(
                json.dumps(error_payload, ensure_ascii=False, indent=2),
                is_error=True,
                error_type=error_type,
            )

        files: list[dict[str, object]] = [
            {
                "path": item.path,
                "action": item.action,
                "hunks": item.hunks,
                "additions": item.additions,
                "removals": item.removals,
                "old_hash": item.old_hash,
                "new_hash": item.new_hash,
            }
            for item in outcome.files
        ]
        payload: dict[str, object] = {
            "dry_run": outcome.dry_run,
            "checkpoint_id": outcome.checkpoint_id,
            "file_count": len(files),
            "files": files,
            "additions": sum(item.additions for item in outcome.files),
            "removals": sum(item.removals for item in outcome.files),
        }
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    # 生成绑定当前工作区状态的逐 hunk 审批计划
    def approval_context(self, params: dict[str, object]) -> dict[str, object] | None:
        request = ApplyPatchParams.model_validate(params)
        return {"patch_plan": self._engine.plan(request.patch).approval_context()}

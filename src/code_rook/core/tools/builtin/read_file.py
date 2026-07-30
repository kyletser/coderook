from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from code_rook.core.editing import content_hash
from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory. "
        "The first line contains path, full-file content_hash and truncation metadata. "
        "Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    def __init__(
        self,
        boundary: WorkspaceBoundary | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        if boundary is not None and workspace_root is not None:
            raise ValueError("pass either boundary or workspace_root, not both")
        self._boundary = boundary or WorkspaceBoundary(workspace_root or Path.cwd())

    # 读取工作区内文件内容；超 512KB 截断
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path
        path = self._boundary.resolve(path_str)
        raw = path.read_bytes()  # raises FileNotFoundError if absent
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"
        metadata = {
            "path": path.relative_to(self._boundary.root).as_posix(),
            "content_hash": content_hash(raw),
            "truncated": truncated,
            "bytes": len(raw),
        }
        header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        return ToolResult(content=f"[metadata] {header}\n[content]\n{text}")

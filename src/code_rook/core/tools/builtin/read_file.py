from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from code_rook.core.editing import content_hash
from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    # 校验一基闭区间，避免反向行范围被误报为空文件
    def validate_line_range(self) -> ReadFileParams:
        if self.end_line is not None and self.end_line < (self.start_line or 1):
            raise ValueError("end_line must be >= start_line")
        return self


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
        "Use start_line/end_line (1-based, inclusive) to read a focused range; "
        "start_line alone reads at most 200 lines. Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            },
            "start_line": {
                "type": "integer", "minimum": 1,
                "description": "First line to read (1-based). Prefer a range for large files.",
            },
            "end_line": {
                "type": "integer", "minimum": 1,
                "description": "Last line to read (inclusive); defaults to start_line + 199.",
            },
        },
        "required": ["path"],
    }

    # 绑定文件读取的工作区边界
    def __init__(
        self,
        boundary: WorkspaceBoundary | None = None,
        *,
        workspace_root: Path | None = None,
    ) -> None:
        if boundary is not None and workspace_root is not None:
            raise ValueError("pass either boundary or workspace_root, not both")
        self._boundary = boundary or WorkspaceBoundary(workspace_root or Path.cwd())

    # 按需读取行范围并保留全文件哈希，未指定范围时兼容原有整文件读取
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = ReadFileParams.model_validate(params)
        path = self._boundary.resolve(parsed.path)
        raw = path.read_bytes()  # raises FileNotFoundError if absent
        selected = raw
        range_metadata: dict[str, object] = {}
        if parsed.start_line is not None or parsed.end_line is not None:
            lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
            start = parsed.start_line or 1
            end = min(parsed.end_line or start + 199, len(lines))
            if start > len(lines) and (lines or start != 1):
                return ToolResult(
                    f"start_line {start} is beyond the file's {len(lines)} lines",
                    is_error=True, error_type="schema_error",
                )
            selected = "".join(lines[start - 1:end]).encode("utf-8")
            range_metadata = {
                "start_line": start, "end_line": end, "total_lines": len(lines),
                "next_line": end + 1 if end < len(lines) and len(selected) <= _MAX_BYTES else None,
            }
        truncated = len(selected) > _MAX_BYTES
        text = selected[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"
        metadata = {
            "path": path.relative_to(self._boundary.root).as_posix(),
            "content_hash": content_hash(raw),
            "truncated": truncated,
            "bytes": len(raw),
            **range_metadata,
        }
        header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        return ToolResult(content=f"[metadata] {header}\n[content]\n{text}")

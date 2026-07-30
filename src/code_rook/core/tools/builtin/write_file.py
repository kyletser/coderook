from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from code_rook.core.checkpoints import CheckpointError, CheckpointStore
from code_rook.core.editing import (
    FileMutation,
    FileTransactionError,
    apply_file_transaction,
    content_hash,
)
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary

_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str


class WriteFileTool(BaseTool):
    params_model = WriteFileParams
    side_effect = ToolSideEffect.LOCAL_WRITE
    name = "write_file"
    description = (
        "Write text content to a file, creating it (and any parent directories) if it "
        "does not exist, or overwriting it if it does. "
        "Path must be relative to the current working directory. "
        "Content size is limited to 1 MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
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
        self._boundary = boundary or WorkspaceBoundary(workspace_root or Path.cwd())
        self._checkpoint_store = checkpoint_store

    # 写入工作区内文件；超 1MB 拒绝；自动创建父目录
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        content = p.content

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        path = self._boundary.resolve(path_str)
        if path.exists() and not path.is_file():
            return ToolResult(
                content=f"path is not a file: {path_str}",
                is_error=True,
                error_type="runtime_error",
            )
        original = path.read_bytes() if path.exists() else None
        mutation = FileMutation(path=path, original=original, updated=encoded)
        checkpoint_id = None
        if self._checkpoint_store is not None:
            try:
                checkpoint_id = self._checkpoint_store.create([mutation], label="write_file")
            except CheckpointError as exc:
                return ToolResult(
                    content=str(exc),
                    is_error=True,
                    error_type="runtime_error",
                )
        try:
            apply_file_transaction(self._boundary.root, [mutation])
        except FileTransactionError as exc:
            if checkpoint_id is not None and self._checkpoint_store is not None:
                self._checkpoint_store.discard(checkpoint_id)
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_type=("conflict" if exc.code == "concurrent_change" else "runtime_error"),
            )
        except BaseException:
            if checkpoint_id is not None and self._checkpoint_store is not None:
                self._checkpoint_store.discard(checkpoint_id)
            raise

        return ToolResult(
            content=(
                f"wrote {len(encoded)} bytes to {path_str} "
                f"(content_hash={content_hash(encoded)}, checkpoint_id={checkpoint_id})"
            )
        )

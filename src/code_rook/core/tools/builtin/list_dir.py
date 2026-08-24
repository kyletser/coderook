from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary

_MAX_DEPTH = 4
_MAX_ENTRIES = 200


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    max_depth: int = Field(default=2, ge=1, le=_MAX_DEPTH)


class ListDirTool(BaseTool):
    params_model = ListDirParams
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    name = "list_dir"
    description = (
        "List the contents of a directory as a tree. "
        "Path must be relative to the current working directory. "
        "Hidden entries (starting with .) are included. "
        f"Maximum depth is {_MAX_DEPTH}, maximum total entries is {_MAX_ENTRIES}."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the directory (default '.').",
            },
            "max_depth": {
                "type": "integer",
                "description": f"How many levels deep to recurse (default 2, max {_MAX_DEPTH}).",
            },
        },
        "required": [],
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

    # 以树状格式列出目录内容，深度和条数有上限
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path_str = p.path
        max_depth = p.max_depth

        root = self._boundary.resolve(path_str)
        if not root.exists():
            raise FileNotFoundError(f"no such directory: {path_str}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines: list[str] = [str(root) + "/"]
        count = 0
        visited = {root.resolve()}

        # 递归目录树且拒绝跟随工作区外符号链接，并对工作区内链接环去重
        def _walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal count
            if depth > max_depth or count >= _MAX_ENTRIES:
                return
            entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
            for i, entry in enumerate(entries):
                if count >= _MAX_ENTRIES:
                    lines.append(f"{prefix}... (truncated)")
                    return
                connector = "└── " if i == len(entries) - 1 else "├── "
                is_directory = False
                recurse_target: Path | None = None
                suffix = ""
                if entry.is_symlink():
                    try:
                        target = entry.resolve(strict=False)
                        target.relative_to(self._boundary.root)
                    except (OSError, RuntimeError, ValueError):
                        suffix = "@ [outside workspace]"
                    else:
                        is_directory = target.is_dir()
                        recurse_target = target if is_directory else None
                        suffix = "@/" if is_directory else "@"
                else:
                    is_directory = entry.is_dir()
                    recurse_target = entry if is_directory else None
                    suffix = "/" if is_directory else ""
                lines.append(f"{prefix}{connector}{entry.name}{suffix}")
                count += 1
                if recurse_target is not None and depth < max_depth:
                    resolved_target = recurse_target.resolve()
                    if resolved_target in visited:
                        continue
                    visited.add(resolved_target)
                    extension = "    " if i == len(entries) - 1 else "│   "
                    _walk(resolved_target, depth + 1, prefix + extension)

        _walk(root, 1, "")
        return ToolResult(content="\n".join(lines))

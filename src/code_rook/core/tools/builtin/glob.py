from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.tools.builtin._search import (
    SearchPathFilter,
    iter_workspace_files,
    resolve_search_root,
    ripgrep_ignore_args,
    ripgrep_path,
    start_process,
    stop_process,
    validate_glob_pattern,
)
from code_rook.core.workspace import WorkspaceBoundary


class GlobParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str
    path: str = "."
    limit: int = Field(default=200, ge=1, le=2000)
    include_hidden: bool = False

    @field_validator("pattern")
    @classmethod
    def _valid_pattern(cls, value: str) -> str:
        return validate_glob_pattern(value)


class GlobTool(BaseTool):
    name = "glob"
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    params_model = GlobParams
    description = (
        "Find files by glob pattern inside the workspace. Supports **, respects ignore files, "
        "returns stable path ordering and explicit truncation metadata."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern such as '**/*.py' or 'src/**/test_*.py'.",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative file or directory to search (default '.').",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "description": "Maximum number of paths to return (default 200).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include hidden files while still respecting ignore rules.",
            },
        },
        "required": ["pattern"],
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

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = GlobParams.model_validate(params)
        search_root, search_arg = resolve_search_root(self._boundary, request.path)
        rg = ripgrep_path()
        if rg is not None:
            return await self._invoke_ripgrep(rg, search_arg, request)
        return self._invoke_python(search_root, request)

    async def _invoke_ripgrep(
        self,
        executable: str,
        search_arg: str,
        request: GlobParams,
    ) -> ToolResult:
        args = [
            "--files",
            "--sort",
            "path",
            "--no-require-git",
        ]
        args.extend(ripgrep_ignore_args(self._boundary.root))
        if request.include_hidden:
            args.append("--hidden")
        else:
            args.extend(("--glob", "!.*", "--glob", "!**/.*"))
        args.extend(("--", search_arg))

        files: list[str] = []
        truncated = False
        search_root, _search_arg = resolve_search_root(self._boundary, request.path)
        path_filter = SearchPathFilter(
            self._boundary.root,
            file_glob=request.pattern,
            include_hidden=request.include_hidden,
            search_root=search_root,
        )
        process = await start_process(executable, args, cwd=self._boundary.root)
        assert process.stdout is not None
        while raw_line := await process.stdout.readline():
            path = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            normalized = Path(path).as_posix()
            if not path_filter.allows(normalized):
                continue
            if len(files) >= request.limit:
                truncated = True
                await stop_process(process)
                break
            files.append(normalized)

        if process.returncode is None:
            _stdout, stderr = await process.communicate()
            return_code = process.returncode or 0
            error = stderr.decode("utf-8", errors="replace").strip()
        else:
            return_code, error = process.returncode, ""
        if return_code not in (0, 1) and not truncated:
            return ToolResult(error or "ripgrep failed", is_error=True, error_type="schema_error")
        return self._result(files, truncated, "ripgrep")

    def _invoke_python(self, search_root: Path, request: GlobParams) -> ToolResult:
        files: list[str] = []
        truncated = False
        for _path, relative in iter_workspace_files(
            self._boundary,
            search_root,
            file_glob=request.pattern,
            include_hidden=request.include_hidden,
        ):
            if len(files) >= request.limit:
                truncated = True
                break
            files.append(relative)
        files.sort(key=lambda value: (value.casefold(), value))
        return self._result(files, truncated, "python")

    @staticmethod
    def _result(files: list[str], truncated: bool, backend: str) -> ToolResult:
        payload = {
            "files": files,
            "count": len(files),
            "truncated": truncated,
            "backend": backend,
        }
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

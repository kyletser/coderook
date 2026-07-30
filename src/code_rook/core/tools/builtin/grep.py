from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.tools.builtin._search import (
    MAX_SEARCH_FILE_BYTES,
    SearchPathFilter,
    decode_rg_text,
    iter_workspace_files,
    read_search_text,
    resolve_search_root,
    ripgrep_ignore_args,
    ripgrep_path,
    start_process,
    stop_process,
    validate_glob_pattern,
)
from code_rook.core.workspace import WorkspaceBoundary

OutputMode = Literal["content", "files_with_matches", "count"]
_MAX_CONTENT_CHARS = 500


class GrepParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str
    path: str = "."
    glob: str | None = None
    output_mode: OutputMode = "content"
    case_sensitive: bool = True
    include_hidden: bool = False
    limit: int = Field(default=100, ge=1, le=2000)

    @field_validator("pattern")
    @classmethod
    def _non_empty_pattern(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("grep pattern must be non-empty")
        return value

    @field_validator("glob")
    @classmethod
    def _valid_glob(cls, value: str | None) -> str | None:
        return validate_glob_pattern(value) if value is not None else None


class GrepTool(BaseTool):
    name = "grep"
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    params_model = GrepParams
    description = (
        "Search text with a regular expression inside the workspace. Returns structured "
        "path/line/column/content matches, respects ignore rules, skips binary and large files, "
        "and reports truncation."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression to search for."},
            "path": {
                "type": "string",
                "description": "Workspace-relative file or directory to search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Optional file glob such as '**/*.py'.",
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": "Result shape (default 'content').",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Use case-sensitive matching (default true).",
            },
            "include_hidden": {
                "type": "boolean",
                "description": "Include hidden files while still respecting ignore rules.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "description": "Maximum records to return (default 100).",
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
        request = GrepParams.model_validate(params)
        search_root, search_arg = resolve_search_root(self._boundary, request.path)
        rg = ripgrep_path()
        if rg is not None:
            return await self._invoke_ripgrep(rg, search_arg, request)
        return self._invoke_python(search_root, request)

    async def _invoke_ripgrep(
        self,
        executable: str,
        search_arg: str,
        request: GrepParams,
    ) -> ToolResult:
        args = [
            "--json",
            "--sort",
            "path",
            "--line-number",
            "--column",
            "--no-require-git",
            "--max-filesize",
            str(MAX_SEARCH_FILE_BYTES),
        ]
        args.extend(ripgrep_ignore_args(self._boundary.root))
        if not request.case_sensitive:
            args.append("--ignore-case")
        if request.include_hidden:
            args.append("--hidden")
        else:
            args.extend(("--glob", "!.*", "--glob", "!**/.*"))
        args.extend(("--", request.pattern, search_arg))

        matches: list[dict[str, Any]] = []
        file_counts: dict[str, int] = defaultdict(int)
        match_count = 0
        truncated = False
        search_root, _search_arg = resolve_search_root(self._boundary, request.path)
        path_filter = SearchPathFilter(
            self._boundary.root,
            file_glob=request.glob,
            include_hidden=request.include_hidden,
            search_root=search_root,
        )
        process = await start_process(executable, args, cwd=self._boundary.root)
        assert process.stdout is not None
        while raw_line := await process.stdout.readline():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data", {})
            path = Path(decode_rg_text(data.get("path"))).as_posix()
            if not path_filter.allows(path):
                continue
            line = decode_rg_text(data.get("lines")).rstrip("\r\n")
            submatches = data.get("submatches", [])
            submatch_count = len(submatches) if isinstance(submatches, list) else 1
            file_counts[path] += submatch_count
            match_count += submatch_count

            if request.output_mode == "content":
                if len(matches) >= request.limit:
                    truncated = True
                    await stop_process(process)
                    break
                start = self._first_match_start(submatches)
                content, content_truncated = self._truncate_content(line)
                matches.append({
                    "path": path,
                    "line": int(data.get("line_number", 0)),
                    "column": self._byte_column(line, start),
                    "content": content,
                    "content_truncated": content_truncated,
                })
            elif request.output_mode == "files_with_matches":
                if len(file_counts) > request.limit:
                    truncated = True
                    await stop_process(process)
                    break

        if process.returncode is None:
            _stdout, stderr = await process.communicate()
            return_code = process.returncode or 0
            error = stderr.decode("utf-8", errors="replace").strip()
        else:
            return_code, error = process.returncode, ""
        if return_code not in (0, 1) and not truncated:
            return ToolResult(error or "ripgrep failed", is_error=True, error_type="schema_error")

        records = self._records_for_mode(request.output_mode, matches, file_counts, request.limit)
        if len(file_counts) > request.limit:
            truncated = True
        return self._result(request.output_mode, records, match_count, truncated, "ripgrep")

    def _invoke_python(self, search_root: Path, request: GrepParams) -> ToolResult:
        flags = 0 if request.case_sensitive else re.IGNORECASE
        try:
            expression = re.compile(request.pattern, flags)
        except re.error as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")

        matches: list[dict[str, Any]] = []
        file_counts: dict[str, int] = defaultdict(int)
        match_count = 0
        truncated = False

        for path, relative in iter_workspace_files(
            self._boundary,
            search_root,
            file_glob=request.glob,
            include_hidden=request.include_hidden,
        ):
            text = read_search_text(path)
            if text is None:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                line_matches = list(expression.finditer(line))
                if not line_matches:
                    continue
                file_counts[relative] += len(line_matches)
                match_count += len(line_matches)
                if request.output_mode == "content":
                    if len(matches) >= request.limit:
                        truncated = True
                        break
                    content, content_truncated = self._truncate_content(line)
                    matches.append({
                        "path": relative,
                        "line": line_number,
                        "column": line_matches[0].start() + 1,
                        "content": content,
                        "content_truncated": content_truncated,
                    })
            if truncated:
                break
            if request.output_mode == "files_with_matches" and len(file_counts) > request.limit:
                truncated = True
                break

        records = self._records_for_mode(request.output_mode, matches, file_counts, request.limit)
        if len(file_counts) > request.limit:
            truncated = True
        return self._result(request.output_mode, records, match_count, truncated, "python")

    @staticmethod
    def _records_for_mode(
        mode: OutputMode,
        matches: list[dict[str, Any]],
        file_counts: dict[str, int],
        limit: int,
    ) -> list[dict[str, Any]]:
        if mode == "content":
            return matches
        paths = sorted(file_counts, key=lambda value: (value.casefold(), value))[:limit]
        if mode == "files_with_matches":
            return [{"path": path} for path in paths]
        return [{"path": path, "matches": file_counts[path]} for path in paths]

    @staticmethod
    def _result(
        mode: OutputMode,
        records: list[dict[str, Any]],
        match_count: int,
        truncated: bool,
        backend: str,
    ) -> ToolResult:
        payload = {
            "mode": mode,
            "matches": records,
            "record_count": len(records),
            "match_count": match_count,
            "truncated": truncated,
            "backend": backend,
        }
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    @staticmethod
    def _first_match_start(submatches: object) -> int:
        if isinstance(submatches, list) and submatches:
            first = submatches[0]
            if isinstance(first, dict):
                return int(first.get("start", 0))
        return 0

    @staticmethod
    def _byte_column(line: str, byte_start: int) -> int:
        prefix = line.encode("utf-8")[:byte_start].decode("utf-8", errors="ignore")
        return len(prefix) + 1

    @staticmethod
    def _truncate_content(content: str) -> tuple[str, bool]:
        if len(content) <= _MAX_CONTENT_CHARS:
            return content, False
        return content[:_MAX_CONTENT_CHARS] + "…", True

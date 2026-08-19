from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from code_rook.core.processes import ProcessSupervisor, terminate_process_tree
from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary

GitDiffScope = Literal["all", "staged", "unstaged"]
_DIFF_LIMIT_BYTES = 50_000
_METADATA_LIMIT_BYTES = 2 * 1024 * 1024
_GIT_TIMEOUT_SECONDS = 15.0


class GitDiffParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scope: GitDiffScope = "all"
    path: str = "."
    diff_limit: int = 50_000

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not value or "\x00" in value:
            raise ValueError("path must be a non-empty workspace-relative path")
        return value

    @field_validator("diff_limit")
    @classmethod
    def _valid_limit(cls, value: int) -> int:
        if not 1_000 <= value <= 200_000:
            raise ValueError("diff_limit must be between 1000 and 200000 bytes")
        return value


class GitDiffError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _GitOutput:
    stdout: bytes
    stderr: str
    return_code: int
    truncated: bool


class GitDiffTool(BaseTool):
    params_model = GitDiffParams
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    name = "git_diff"
    description = (
        "Inspect Git working tree changes without modifying the repository. Returns structured "
        "changed-file status, additions/deletions and a bounded unified diff. Includes untracked "
        "files in status and supports all, staged or unstaged scopes."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["all", "staged", "unstaged"],
                "description": "Changes to inspect (default 'all').",
            },
            "path": {
                "type": "string",
                "description": "Optional workspace-relative path filter (default '.').",
            },
            "diff_limit": {
                "type": "integer",
                "minimum": 1000,
                "maximum": 200000,
                "description": "Maximum diff bytes returned (default 50000).",
            },
        },
    }

    def __init__(
        self,
        boundary: WorkspaceBoundary | None = None,
        *,
        workspace_root: Path | None = None,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        if boundary is not None and workspace_root is not None:
            raise ValueError("pass either boundary or workspace_root, not both")
        self._boundary = boundary or WorkspaceBoundary(workspace_root or Path.cwd())
        self._git = shutil.which("git")
        self._process_supervisor = process_supervisor

    # 通过统一监管器终止 Git 进程树，无监管器时执行平台原生回收
    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if self._process_supervisor is not None:
            await self._process_supervisor.terminate(process)
        else:
            await terminate_process_tree(process)

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = GitDiffParams.model_validate(params)
        try:
            payload = await self._inspect(request)
        except GitDiffError as exc:
            return ToolResult(
                json.dumps(
                    {"error": {"code": exc.code, "message": str(exc)}},
                    ensure_ascii=False,
                    indent=2,
                ),
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    async def _inspect(self, request: GitDiffParams) -> dict[str, object]:
        if self._git is None:
            raise GitDiffError("git_not_found", "git executable was not found on PATH")

        requested_path = self._boundary.resolve(request.path)
        path_arg = requested_path.relative_to(self._boundary.root).as_posix() or "."
        root_result = await self._git_run(
            ["rev-parse", "--show-toplevel"],
            limit=_METADATA_LIMIT_BYTES,
        )
        if root_result.return_code != 0:
            raise GitDiffError(
                "not_git_repository",
                root_result.stderr or "workspace is not a Git repository",
            )
        repository = Path(root_result.stdout.decode("utf-8", errors="replace").strip()).resolve()
        if not self._boundary.root.resolve().is_relative_to(repository):
            raise GitDiffError(
                "repository_outside_workspace",
                "Git repository root must contain the workspace root",
            )
        workspace_prefix = self._boundary.root.resolve().relative_to(repository).as_posix()

        has_head = (
            await self._git_run(
                ["rev-parse", "--verify", "HEAD"],
                limit=_METADATA_LIMIT_BYTES,
            )
        ).return_code == 0
        status_result = await self._git_run(
            [
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                path_arg,
            ],
            limit=_METADATA_LIMIT_BYTES,
        )
        if status_result.return_code != 0:
            raise GitDiffError("git_status_failed", status_result.stderr or "git status failed")
        if status_result.truncated:
            raise GitDiffError("git_status_too_large", "git status exceeded the metadata limit")
        files = _parse_status(status_result.stdout, request.scope)
        for file_info in files:
            file_info["path"] = _relativize(str(file_info["path"]), workspace_prefix)
            if file_info.get("original_path"):
                file_info["original_path"] = _relativize(
                    str(file_info["original_path"]), workspace_prefix
                )

        diff_outputs: list[_GitOutput] = []
        for diff_args in _diff_arg_sets(request.scope, has_head, path_arg):
            output = await self._git_run(diff_args, limit=request.diff_limit)
            if output.return_code not in {0, 1} and not output.truncated:
                raise GitDiffError("git_diff_failed", output.stderr or "git diff failed")
            diff_outputs.append(output)
        combined_diff = b"".join(output.stdout for output in diff_outputs)
        diff_truncated = any(output.truncated for output in diff_outputs) or (
            len(combined_diff) > request.diff_limit
        )
        diff = combined_diff[: request.diff_limit].decode("utf-8", errors="replace")
        if diff_truncated:
            diff += "\n[diff truncated]\n"

        stats: dict[str, tuple[int | None, int | None]] = {}
        for numstat_args in _diff_arg_sets(request.scope, has_head, path_arg, numstat=True):
            output = await self._git_run(numstat_args, limit=_METADATA_LIMIT_BYTES)
            if output.return_code not in {0, 1}:
                raise GitDiffError("git_numstat_failed", output.stderr or "git numstat failed")
            if output.truncated:
                raise GitDiffError("git_numstat_too_large", "git numstat exceeded its limit")
            _merge_stats(stats, _parse_numstat(output.stdout))
        stats = {
            _relativize(path, workspace_prefix): counts for path, counts in stats.items()
        }
        for file_info in files:
            additions, deletions = stats.get(str(file_info["path"]), (None, None))
            file_info["additions"] = additions
            file_info["deletions"] = deletions

        known_stats = [values for values in stats.values() if values[0] is not None]
        return {
            "repository": ".",
            "scope": request.scope,
            "path": path_arg,
            "has_head": has_head,
            "files": files,
            "file_count": len(files),
            "additions": sum(value[0] or 0 for value in known_stats),
            "deletions": sum(value[1] or 0 for value in known_stats),
            "diff": diff,
            "diff_truncated": diff_truncated,
        }

    async def _git_run(self, args: list[str], *, limit: int) -> _GitOutput:
        assert self._git is not None
        environment = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if self._process_supervisor is not None:
            process = await self._process_supervisor.start_exec(
                self._git,
                *args,
                label="git-diff",
                cwd=self._boundary.root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                self._git,
                *args,
                cwd=self._boundary.root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        assert process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        chunks: list[bytes] = []
        size = 0
        truncated = False
        try:
            async with asyncio.timeout(_GIT_TIMEOUT_SECONDS):
                while chunk := await process.stdout.read(8192):
                    remaining = limit - size
                    if remaining <= 0:
                        truncated = True
                        await self._terminate_process(process)
                        break
                    chunks.append(chunk[:remaining])
                    size += min(len(chunk), remaining)
                    if len(chunk) > remaining:
                        truncated = True
                        await self._terminate_process(process)
                        break
                await process.wait()
                stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        except TimeoutError as exc:
            await self._terminate_process(process)
            await stderr_task
            raise GitDiffError("git_timeout", "git command timed out") from exc
        except asyncio.CancelledError:
            await self._terminate_process(process)
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            raise
        finally:
            if self._process_supervisor is not None and process.returncode is not None:
                self._process_supervisor.forget(process)
        return _GitOutput(
            stdout=b"".join(chunks),
            stderr=stderr,
            return_code=process.returncode or 0,
            truncated=truncated,
        )


def _diff_args(
    scope: GitDiffScope,
    has_head: bool,
    path: str,
    *,
    numstat: bool = False,
) -> list[str]:
    args = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames"]
    if numstat:
        args.extend(("--numstat", "-z"))
    if scope == "staged" or (scope == "all" and not has_head):
        args.append("--cached")
    elif scope == "all":
        args.append("HEAD")
    args.extend(("--", path))
    return args


def _relativize(path: str, workspace_prefix: str) -> str:
    if workspace_prefix == ".":
        return path
    needle = workspace_prefix + "/"
    if path.startswith(needle):
        return path[len(needle) :]
    return path


def _diff_arg_sets(
    scope: GitDiffScope,
    has_head: bool,
    path: str,
    *,
    numstat: bool = False,
) -> list[list[str]]:
    if scope == "all" and not has_head:
        return [
            _diff_args("staged", has_head, path, numstat=numstat),
            _diff_args("unstaged", has_head, path, numstat=numstat),
        ]
    return [_diff_args(scope, has_head, path, numstat=numstat)]


def _parse_status(raw: bytes, scope: GitDiffScope) -> list[dict[str, object]]:
    tokens = raw.split(b"\0")
    files: list[dict[str, object]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        decoded = token.decode("utf-8", errors="replace")
        if len(decoded) < 3:
            continue
        index_status, worktree_status = decoded[0], decoded[1]
        path = decoded[3:].replace("\\", "/")
        original_path: str | None = None
        if index_status in {"R", "C"} or worktree_status in {"R", "C"}:
            if index < len(tokens):
                original_path = tokens[index].decode("utf-8", errors="replace").replace(
                    "\\", "/"
                )
                index += 1

        untracked = index_status == "?" and worktree_status == "?"
        staged = not untracked and index_status not in {" ", "?"}
        unstaged = untracked or worktree_status not in {" ", "?"}
        if scope == "staged" and not staged:
            continue
        if scope == "unstaged" and not unstaged:
            continue
        files.append({
            "path": path,
            "original_path": original_path,
            "index_status": index_status,
            "worktree_status": worktree_status,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "additions": None,
            "deletions": None,
        })
    files.sort(key=lambda item: (str(item["path"]).casefold(), str(item["path"])))
    return files


def _parse_numstat(raw: bytes) -> dict[str, tuple[int | None, int | None]]:
    tokens = raw.split(b"\0")
    stats: dict[str, tuple[int | None, int | None]] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        fields = token.split(b"\t", maxsplit=2)
        if len(fields) != 3:
            continue
        additions = _parse_count(fields[0])
        deletions = _parse_count(fields[1])
        if fields[2]:
            path = fields[2].decode("utf-8", errors="replace")
        elif index + 1 < len(tokens):
            index += 1  # original rename path
            path = tokens[index].decode("utf-8", errors="replace")
            index += 1
        else:
            continue
        stats[path.replace("\\", "/")] = (additions, deletions)
    return stats


def _parse_count(raw: bytes) -> int | None:
    return int(raw) if raw.isdigit() else None


def _merge_stats(
    destination: dict[str, tuple[int | None, int | None]],
    incoming: dict[str, tuple[int | None, int | None]],
) -> None:
    for path, values in incoming.items():
        previous = destination.get(path)
        if previous is None:
            destination[path] = values
            continue
        additions = None if previous[0] is None or values[0] is None else previous[0] + values[0]
        deletions = None if previous[1] is None or values[1] is None else previous[1] + values[1]
        destination[path] = (additions, deletions)

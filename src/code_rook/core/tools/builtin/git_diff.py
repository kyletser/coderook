from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from code_rook.core.processes import (
    ProcessSupervisor,
    sanitized_shell_environment,
    terminate_process_tree,
)
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
    # 保存稳定错误码和不含仓库正文的 Git diff 失败原因
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _GitOutput:
    stdout: bytes
    stderr: str
    return_code: int
    truncated: bool


@dataclass(frozen=True)
class _UntrackedReview:
    diff: bytes
    status: str
    complete: bool
    note: str
    size: int | None
    sha256: str | None
    additions: int | None
    truncated: bool = False


@dataclass(frozen=True)
class _BlobEvidence:
    size: int
    sha256: str


@dataclass(frozen=True)
class _TrackedReview:
    status: str
    complete: bool
    note: str
    evidence: bytes
    old_present: bool
    new_present: bool
    old_blob: _BlobEvidence | None
    new_blob: _BlobEvidence | None


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

    # 连续生成两份完全相同的结构化审查结果，仓库竞态变化时失败关闭
    async def _inspect(self, request: GitDiffParams) -> dict[str, object]:
        first = await self._inspect_once(request)
        second = await self._inspect_once(request)
        if first != second:
            raise GitDiffError(
                "repository_changed_during_review",
                "repository changed while preparing the visible diff; retry review",
            )
        return second

    # 从当前仓库状态生成一份结构化文件清单、审查完整性和可见补丁
    async def _inspect_once(self, request: GitDiffParams) -> dict[str, object]:
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
            raw_path = str(file_info["path"])
            raw_original = file_info.get("original_path")
            workspace_path = _relativize(raw_path, workspace_prefix)
            if workspace_prefix != "." and workspace_path == raw_path:
                raise GitDiffError(
                    "git_status_outside_workspace",
                    "git status returned a path outside the workspace",
                )
            file_info["_repository_path"] = raw_path
            file_info["_repository_original_path"] = (
                str(raw_original) if raw_original else None
            )
            file_info["path"] = workspace_path
            if raw_original:
                original_workspace_path = _relativize(
                    str(raw_original), workspace_prefix
                )
                if (
                    workspace_prefix != "."
                    and original_workspace_path == str(raw_original)
                ):
                    raise GitDiffError(
                        "git_status_outside_workspace",
                        "git rename or copy originated outside the workspace",
                    )
                file_info["original_path"] = original_workspace_path

        diff_outputs: list[_GitOutput] = []
        for diff_args in _diff_arg_sets(request.scope, has_head, path_arg):
            output = await self._git_run(diff_args, limit=request.diff_limit)
            if output.return_code not in {0, 1} and not output.truncated:
                raise GitDiffError("git_diff_failed", output.stderr or "git diff failed")
            diff_outputs.append(output)
        raw_tracked_diff = b"".join(output.stdout for output in diff_outputs)
        tracked_diff_truncated = any(output.truncated for output in diff_outputs) or (
            len(raw_tracked_diff) > request.diff_limit
        )
        combined_diff = raw_tracked_diff[: request.diff_limit]

        stats: dict[str, tuple[int | None, int | None]] = {}
        for numstat_args in _diff_arg_sets(request.scope, has_head, path_arg, numstat=True):
            output = await self._git_run(numstat_args, limit=_METADATA_LIMIT_BYTES)
            if output.return_code not in {0, 1}:
                raise GitDiffError("git_numstat_failed", output.stderr or "git numstat failed")
            if output.truncated:
                raise GitDiffError("git_numstat_too_large", "git numstat exceeded its limit")
            _merge_stats(stats, _parse_numstat(output.stdout))
        # diff --relative 已保证 numstat 与公开文件清单都使用 workspace 相对路径
        stats = dict(stats)

        opaque_paths = _invalid_utf8_diff_paths(
            raw_tracked_diff,
            {str(file_info["path"]) for file_info in files},
        )
        tracked_reviews: dict[str, _TrackedReview] = {}
        remaining_diff_bytes = max(0, request.diff_limit - len(combined_diff))
        for file_info in files:
            if bool(file_info.get("untracked", False)):
                continue
            path = str(file_info["path"])
            additions, deletions = stats.get(path, (None, None))
            if (
                additions is not None
                and deletions is not None
                and path not in opaque_paths
                and "*" not in opaque_paths
            ):
                continue
            review = await self._review_tracked_opaque(
                file_info,
                scope=request.scope,
                has_head=has_head,
                status=(
                    "binary"
                    if additions is None or deletions is None
                    else "opaque"
                ),
            )
            if review.complete and len(review.evidence) <= remaining_diff_bytes:
                combined_diff += review.evidence
                remaining_diff_bytes -= len(review.evidence)
            elif review.complete:
                review = _TrackedReview(
                    status=review.status,
                    complete=False,
                    note=(
                        "Review blocked: opaque blob evidence does not fit the remaining "
                        "visible diff budget"
                    ),
                    evidence=b"",
                    old_present=review.old_present,
                    new_present=review.new_present,
                    old_blob=review.old_blob,
                    new_blob=review.new_blob,
                )
                tracked_diff_truncated = True
            tracked_reviews[path] = review

        untracked_reviews: dict[str, _UntrackedReview] = {}
        for file_info in files:
            if not bool(file_info.get("untracked", False)):
                continue
            path = str(file_info["path"])
            untracked_review = await self._review_untracked(
                path,
                source_limit=request.diff_limit,
                diff_limit=remaining_diff_bytes,
            )
            untracked_reviews[path] = untracked_review
            if untracked_review.diff:
                combined_diff += untracked_review.diff
                remaining_diff_bytes = max(
                    0,
                    remaining_diff_bytes - len(untracked_review.diff),
                )

        diff_truncated = tracked_diff_truncated or any(
            review.truncated for review in untracked_reviews.values()
        )
        diff = combined_diff.decode("utf-8", errors="replace")
        if diff_truncated:
            diff += "\n[diff truncated]\n"
        for file_info in files:
            path = str(file_info["path"])
            file_untracked_review = untracked_reviews.get(path)
            if file_untracked_review is not None:
                additions = file_untracked_review.additions
                deletions = 0 if file_untracked_review.additions is not None else None
                file_info["review_status"] = file_untracked_review.status
                file_info["review_complete"] = file_untracked_review.complete
                file_info["review_note"] = file_untracked_review.note
                file_info["content_size"] = file_untracked_review.size
                file_info["content_sha256"] = file_untracked_review.sha256
                if additions is not None:
                    stats[path] = (additions, 0)
            else:
                additions, deletions = stats.get(path, (None, None))
                tracked_review = tracked_reviews.get(path)
                if tracked_review is not None:
                    file_info["review_status"] = tracked_review.status
                    file_info["review_complete"] = (
                        tracked_review.complete and not tracked_diff_truncated
                    )
                    file_info["review_note"] = (
                        "Review blocked: tracked diff exceeded the visible diff limit"
                        if tracked_diff_truncated and tracked_review.complete
                        else tracked_review.note
                    )
                    _store_blob_evidence(
                        file_info,
                        "old",
                        tracked_review.old_blob,
                        present=tracked_review.old_present,
                    )
                    _store_blob_evidence(
                        file_info,
                        "new",
                        tracked_review.new_blob,
                        present=tracked_review.new_present,
                    )
                    file_info["content_size"] = (
                        tracked_review.new_blob.size
                        if tracked_review.new_blob is not None
                        else None
                    )
                    file_info["content_sha256"] = (
                        tracked_review.new_blob.sha256
                        if tracked_review.new_blob is not None
                        else None
                    )
                else:
                    file_info["review_status"] = (
                        "truncated" if tracked_diff_truncated else "text"
                    )
                    file_info["review_complete"] = not tracked_diff_truncated
                    file_info["review_note"] = (
                        "Review blocked: tracked diff exceeded the visible diff limit"
                        if tracked_diff_truncated
                        else "Complete Git text or metadata diff"
                    )
                    file_info["content_size"] = None
                    file_info["content_sha256"] = None
            file_info["additions"] = additions
            file_info["deletions"] = deletions
            file_info.pop("_repository_path", None)
            file_info.pop("_repository_original_path", None)

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

    # 为 tracked 二进制或非 UTF-8 变更生成完整前后 blob 证据
    async def _review_tracked_opaque(
        self,
        file_info: dict[str, object],
        *,
        scope: GitDiffScope,
        has_head: bool,
        status: str,
    ) -> _TrackedReview:
        path = str(file_info["path"])
        repository_path = str(file_info.get("_repository_path") or path)
        original_path = str(
            file_info.get("_repository_original_path") or repository_path
        )
        index_status = str(file_info.get("index_status", " "))
        worktree_status = str(file_info.get("worktree_status", " "))

        if scope == "unstaged":
            old_present = True
            old_spec = f":{original_path}"
        else:
            old_present = has_head and index_status != "A"
            old_spec = f"HEAD:{original_path}"
        if scope == "staged":
            new_present = index_status != "D"
            new_spec: str | None = f":{repository_path}"
        else:
            new_present = worktree_status != "D" and not (
                scope == "all" and index_status == "D" and worktree_status == " "
            )
            new_spec = None

        old_blob: _BlobEvidence | None = None
        new_blob: _BlobEvidence | None = None
        errors: list[str] = []
        if old_present:
            old_blob, error = await self._read_git_blob(old_spec)
            if error is not None:
                errors.append(f"old blob: {error}")
        if new_present:
            if new_spec is not None:
                new_blob, error = await self._read_git_blob(new_spec)
            else:
                new_blob, error = await asyncio.to_thread(
                    self._read_workspace_blob_sync,
                    path,
                )
            if error is not None:
                errors.append(f"new blob: {error}")
        if errors:
            return _TrackedReview(
                status=status,
                complete=False,
                note="Review blocked: " + "; ".join(errors),
                evidence=b"",
                old_present=old_present,
                new_present=new_present,
                old_blob=old_blob,
                new_blob=new_blob,
            )

        old_description = _blob_description(old_blob, present=old_present)
        new_description = _blob_description(new_blob, present=new_present)
        note = (
            f"Opaque tracked content review: old {old_description}; "
            f"new {new_description}"
        )
        evidence = _tracked_evidence_diff(
            path,
            old_description=old_description,
            new_description=new_description,
        )
        return _TrackedReview(
            status=status,
            complete=True,
            note=note,
            evidence=evidence,
            old_present=old_present,
            new_present=new_present,
            old_blob=old_blob,
            new_blob=new_blob,
        )

    # 从 Git 对象库读取完整 blob 并计算字节长度与 SHA-256
    async def _read_git_blob(
        self,
        spec: str,
    ) -> tuple[_BlobEvidence | None, str | None]:
        output = await self._git_run(["show", spec], limit=_METADATA_LIMIT_BYTES)
        if output.return_code != 0:
            return None, output.stderr or "Git object is unavailable"
        if output.truncated:
            return None, f"blob exceeds {_METADATA_LIMIT_BYTES} byte evidence limit"
        return _blob_evidence(output.stdout), None

    # 安全读取 workspace 中 tracked 文件或符号链接的完整 blob 字节
    def _read_workspace_blob_sync(
        self,
        path: str,
    ) -> tuple[_BlobEvidence | None, str | None]:
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any(character in path for character in "\r\n\x00")
        ):
            return None, "unsafe workspace path"
        current = self._boundary.root
        for part in pure.parts[:-1]:
            current /= part
            try:
                parent = os.lstat(current)
            except OSError:
                return None, "path changed while being inspected"
            if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
                return None, "path crosses a symbolic link"
        target = self._boundary.root.joinpath(*pure.parts)
        try:
            before = os.lstat(target)
        except OSError:
            return None, "path is unavailable"
        if stat.S_ISLNK(before.st_mode):
            try:
                data = os.fsencode(os.readlink(target))
                after = os.lstat(target)
            except OSError:
                return None, "symbolic link changed while being inspected"
            if not _same_file_snapshot(before, after):
                return None, "symbolic link changed while being inspected"
            return _blob_evidence(data), None
        if not stat.S_ISREG(before.st_mode):
            return None, "path is not a regular file or symbolic link"
        if before.st_size > _METADATA_LIMIT_BYTES:
            return None, f"blob exceeds {_METADATA_LIMIT_BYTES} byte evidence limit"
        try:
            with target.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or not _same_file_identity(before, opened):
                    return None, "file was replaced before reading"
                data = handle.read(_METADATA_LIMIT_BYTES + 1)
                finished = os.fstat(handle.fileno())
            after = os.lstat(target)
        except OSError:
            return None, "file changed while being inspected"
        if (
            len(data) > _METADATA_LIMIT_BYTES
            or not _same_file_snapshot(opened, finished)
            or not _same_file_snapshot(before, after)
            or not _same_file_identity(finished, after)
        ):
            return None, "file changed while being inspected"
        return _blob_evidence(data), None

    # 在线程中读取未跟踪项并生成不会触碰 Git index 的有界审查表示
    async def _review_untracked(
        self,
        path: str,
        *,
        source_limit: int,
        diff_limit: int,
    ) -> _UntrackedReview:
        return await asyncio.to_thread(
            self._review_untracked_sync,
            path,
            source_limit=source_limit,
            diff_limit=diff_limit,
        )

    # 安全读取未跟踪文件正文，文本生成完整补丁，二进制只暴露长度与摘要
    def _review_untracked_sync(
        self,
        path: str,
        *,
        source_limit: int,
        diff_limit: int,
    ) -> _UntrackedReview:
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or any(character in path for character in "\r\n\x00")
        ):
            return _unavailable_untracked_review("Review blocked: unsafe Git path")

        current = self._boundary.root
        for part in pure.parts[:-1]:
            current /= part
            try:
                parent = os.lstat(current)
            except OSError:
                return _unavailable_untracked_review(
                    "Review blocked: untracked path changed while being inspected"
                )
            if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
                return _unavailable_untracked_review(
                    "Review blocked: untracked path crosses a symbolic link"
                )

        target = self._boundary.root.joinpath(*pure.parts)
        try:
            before = os.lstat(target)
        except OSError:
            return _unavailable_untracked_review(
                "Review blocked: untracked path changed while being inspected"
            )
        if stat.S_ISLNK(before.st_mode):
            try:
                link_target = os.readlink(target)
                after = os.lstat(target)
            except OSError:
                return _unavailable_untracked_review(
                    "Review blocked: symbolic link changed while being inspected"
                )
            if not _same_file_snapshot(before, after):
                return _unavailable_untracked_review(
                    "Review blocked: symbolic link changed while being inspected"
                )
            data = os.fsencode(link_target)
            return _build_untracked_review(
                path,
                data,
                mode="120000",
                source_limit=source_limit,
                diff_limit=diff_limit,
            )
        if not stat.S_ISREG(before.st_mode):
            return _unavailable_untracked_review(
                "Review blocked: untracked entry is not a regular file or symbolic link"
            )
        if before.st_size > source_limit:
            return _oversized_untracked_review(int(before.st_size), source_limit)

        try:
            with target.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                if not stat.S_ISREG(opened.st_mode) or not _same_file_identity(before, opened):
                    return _unavailable_untracked_review(
                        "Review blocked: untracked file was replaced before reading"
                    )
                data = handle.read(source_limit + 1)
                finished = os.fstat(handle.fileno())
            after = os.lstat(target)
        except OSError:
            return _unavailable_untracked_review(
                "Review blocked: untracked file changed while being inspected"
            )
        if (
            len(data) > source_limit
            or not _same_file_snapshot(opened, finished)
            or not _same_file_snapshot(before, after)
            or not _same_file_identity(finished, after)
        ):
            return _unavailable_untracked_review(
                "Review blocked: untracked file changed while being inspected"
            )
        return _build_untracked_review(
            path,
            data,
            mode=(
                "100755"
                if before.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                else "100644"
            ),
            source_limit=source_limit,
            diff_limit=diff_limit,
        )

    async def _git_run(self, args: list[str], *, limit: int) -> _GitOutput:
        assert self._git is not None
        environment = sanitized_shell_environment()
        environment.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        command = (
            self._git,
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.quotepath=false",
            *args,
        )
        if self._process_supervisor is not None:
            process = await self._process_supervisor.start_exec(
                *command,
                label="git-diff",
                cwd=self._boundary.root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
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


# 比较文件类型、设备号和 inode，识别读取前后的路径替换
def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


# 比较文件身份、长度和纳秒时间，识别审查读取期间的并发写入
def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        _same_file_identity(left, right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


# 计算完整 blob 的长度与 SHA-256 审查证据
def _blob_evidence(data: bytes) -> _BlobEvidence:
    return _BlobEvidence(size=len(data), sha256=hashlib.sha256(data).hexdigest())


# 将存在或缺失的 blob 转为不含正文的稳定可见描述
def _blob_description(blob: _BlobEvidence | None, *, present: bool) -> str:
    if not present:
        return "absent"
    if blob is None:
        return "unavailable"
    return f"{blob.size} bytes SHA-256 {blob.sha256}"


# 为不透明 tracked 文件生成纳入可见预算的 Git 风格证据块
def _tracked_evidence_diff(
    path: str,
    *,
    old_description: str,
    new_description: str,
) -> bytes:
    old_path = shlex.quote(f"a/{path}")
    new_path = shlex.quote(f"b/{path}")
    return (
        f"diff --git {old_path} {new_path}\n"
        "CodeRook opaque blob evidence\n"
        f"old blob: {old_description}\n"
        f"new blob: {new_description}\n"
    ).encode()


# 将前后 blob 的存在性、长度和摘要写入公开文件审查记录
def _store_blob_evidence(
    file_info: dict[str, object],
    side: str,
    blob: _BlobEvidence | None,
    *,
    present: bool,
) -> None:
    file_info[f"{side}_content_present"] = present
    file_info[f"{side}_content_size"] = blob.size if blob is not None else None
    file_info[f"{side}_content_sha256"] = blob.sha256 if blob is not None else None


# 定位包含非法 UTF-8 正文的 tracked diff 文件，无法精确绑定路径时阻断全部文本审查
def _invalid_utf8_diff_paths(raw: bytes, known_paths: set[str]) -> set[str]:
    invalid: set[str] = set()
    for block in raw.split(b"diff --git ")[1:]:
        try:
            block.decode("utf-8")
            continue
        except UnicodeDecodeError:
            pass
        header = block.splitlines()[0].decode("utf-8", errors="replace")
        try:
            parts = shlex.split(header)
        except ValueError:
            parts = []
        if len(parts) != 2:
            invalid.add("*")
            continue
        path = parts[-1]
        if path.startswith("b/"):
            path = path[2:]
        normalized = path
        if normalized not in known_paths:
            invalid.add("*")
            continue
        invalid.add(normalized)
    return invalid


# 构造无法安全展示正文的未跟踪项结果并禁止后续变更动作
def _unavailable_untracked_review(note: str) -> _UntrackedReview:
    return _UntrackedReview(
        diff=b"",
        status="unavailable",
        complete=False,
        note=note,
        size=None,
        sha256=None,
        additions=None,
    )


# 构造超过可见审查上限的未跟踪文件结果并保留安全长度提示
def _oversized_untracked_review(size: int, limit: int) -> _UntrackedReview:
    return _UntrackedReview(
        diff=b"",
        status="truncated",
        complete=False,
        note=(
            f"Review blocked: file is {size} bytes and exceeds the "
            f"{limit} byte visible diff limit"
        ),
        size=size,
        sha256=None,
        additions=None,
        truncated=True,
    )


# 将 UTF-8 文本规范化为可安全渲染的换行形式，控制字符内容按二进制处理
def _safe_text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    normalized = text.replace("\r\n", "\n")
    if "\r" in normalized:
        return None
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
        return None
    return normalized


# 为完整未跟踪 UTF-8 文本生成只新增内容的标准 unified diff
def _untracked_text_diff(path: str, text: str, *, mode: str) -> tuple[bytes, int]:
    lines = text.split("\n")
    has_final_newline = text.endswith("\n")
    if has_final_newline:
        lines = lines[:-1]
    additions = len(lines) if text else 0
    quoted_path = shlex.quote(f"b/{path}")
    output = [
        f"diff --git {shlex.quote(f'a/{path}')} {quoted_path}",
        f"new file mode {mode}",
        "--- /dev/null",
        f"+++ {quoted_path}",
    ]
    if additions:
        count = "" if additions == 1 else f",{additions}"
        output.append(f"@@ -0,0 +1{count} @@")
        output.extend(f"+{line}" for line in lines)
        if not has_final_newline:
            output.append("\\ No newline at end of file")
    return ("\n".join(output) + "\n").encode("utf-8"), additions


# 根据完整字节生成文本补丁或二进制摘要，并在预算不足时显式阻断
def _build_untracked_review(
    path: str,
    data: bytes,
    *,
    mode: str,
    source_limit: int,
    diff_limit: int,
) -> _UntrackedReview:
    size = len(data)
    if size > source_limit:
        return _oversized_untracked_review(size, source_limit)
    digest = hashlib.sha256(data).hexdigest()
    text = _safe_text(data)
    if text is None:
        return _UntrackedReview(
            diff=b"",
            status="binary",
            complete=True,
            note=f"Binary content review: {size} bytes; SHA-256 {digest}",
            size=size,
            sha256=digest,
            additions=None,
        )
    diff, additions = _untracked_text_diff(path, text, mode=mode)
    if len(diff) > diff_limit:
        return _UntrackedReview(
            diff=b"",
            status="truncated",
            complete=False,
            note=(
                "Review blocked: complete text diff does not fit the remaining "
                f"visible diff budget; {size} bytes; SHA-256 {digest}"
            ),
            size=size,
            sha256=digest,
            additions=additions,
            truncated=True,
        )
    return _UntrackedReview(
        diff=diff,
        status="text",
        complete=True,
        note=f"Full UTF-8 text review: {size} bytes; SHA-256 {digest}",
        size=size,
        sha256=digest,
        additions=additions,
    )


def _diff_args(
    scope: GitDiffScope,
    has_head: bool,
    path: str,
    *,
    numstat: bool = False,
) -> list[str]:
    args = [
        "diff",
        "--relative",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
    ]
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
        path = decoded[3:]
        original_path: str | None = None
        if index_status in {"R", "C"} or worktree_status in {"R", "C"}:
            if index < len(tokens):
                original_path = tokens[index].decode("utf-8", errors="replace")
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
        stats[path] = (additions, deletions)
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

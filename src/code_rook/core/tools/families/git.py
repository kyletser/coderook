from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_rook.core.processes import terminate_process_tree
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolSpec,
)
from code_rook.core.workspace import WorkspaceBoundary

_OUTPUT_LIMIT = 200_000
_STDERR_LIMIT = 20_000
_TIMEOUT_SECONDS = 15.0


class _StatusParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = "."
    include_untracked: bool = True


class _LogParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    limit: int = Field(default=20, ge=1, le=100)
    path: str = "."


class _ShowParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    revision: str = Field(min_length=1, pattern=r"^[^-\x00].*")
    path: str = "."


class _BlameParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str = Field(min_length=1)
    revision: str = Field(default="HEAD", min_length=1, pattern=r"^[^-\x00].*")
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    # 确保 blame 行号范围完整且递增
    def validate_range(self) -> _BlameParams:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.start_line > self.end_line
        ):
            raise ValueError("start_line must not exceed end_line")
        return self


@dataclass(frozen=True)
class _GitOutput:
    stdout: str
    stderr: str
    return_code: int
    truncated: bool


class GitTool(BaseTool):
    name = "Git"
    description = (
        "Inspect Git status, diff, log, revisions, and blame information without modifying "
        "the repository. Choose one read-only action."
    )
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 初始化只读 Git family 和兼容 diff backend
    def __init__(self, boundary: WorkspaceBoundary, diff_backend: GitDiffTool) -> None:
        self._boundary = boundary
        self._diff = diff_backend
        self._git = shutil.which("git")

    # 返回 Git family 的 action 级 schema
    def build_spec(self) -> ToolSpec:
        capabilities = frozenset({ToolCapability.READ, ToolCapability.GIT})
        actions = (
            ToolActionSpec(
                name="status",
                description="Show bounded branch and working tree status.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "include_untracked": {"type": "boolean"},
                    },
                },
                capabilities=capabilities,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="diff",
                description=self._diff.description,
                input_schema=self._diff.input_schema,
                capabilities=capabilities,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="log",
                description="Show a bounded commit log, optionally filtered by path.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "path": {"type": "string"},
                    },
                },
                capabilities=capabilities,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="show",
                description="Show a revision with bounded metadata, stats, and patch.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "revision": {"type": "string"},
                        "path": {"type": "string"},
                    },
                    "required": ["revision"],
                },
                capabilities=capabilities,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="blame",
                description="Show bounded line attribution for a workspace file.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "revision": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
                capabilities=capabilities,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            actions=actions,
            capabilities=capabilities,
            approval_requirement=ApprovalRequirement.NEVER,
            parallel_policy=ParallelPolicy.SAFE,
        )

    # 校验 Git 仓库根目录必须与 workspace 完全一致
    async def _ensure_repository(self) -> ToolResult | None:
        output = await self._run(["rev-parse", "--show-toplevel"])
        if output.return_code != 0:
            return ToolResult(
                output.stderr or "workspace is not a Git repository",
                is_error=True,
                error_type="runtime_error",
            )
        repository = Path(output.stdout.strip()).resolve()
        if repository != self._boundary.root:
            return ToolResult(
                "Git repository root must match the workspace root",
                is_error=True,
                error_type="permission_denied",
            )
        return None

    # 解析 workspace path 为传给 Git 的相对路径参数
    def _path(self, value: str) -> str:
        path = self._boundary.resolve(value)
        return path.relative_to(self._boundary.root).as_posix() or "."

    # 有界读取单个子进程流，超限时终止进程树防止管道和内存继续增长
    async def _read_limited(
        self,
        stream: asyncio.StreamReader,
        limit: int,
        process: asyncio.subprocess.Process,
    ) -> tuple[bytes, bool]:
        chunks: list[bytes] = []
        size = 0
        while chunk := await stream.read(8192):
            remaining = limit - size
            if remaining <= 0:
                await terminate_process_tree(process)
                return b"".join(chunks), True
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
            if len(chunk) > remaining:
                await terminate_process_tree(process)
                return b"".join(chunks), True
        return b"".join(chunks), False

    # 执行有界、无交互、禁用 pager 和 optional locks 的 Git 子进程
    async def _run(self, args: list[str]) -> _GitOutput:
        if self._git is None:
            return _GitOutput("", "git executable was not found on PATH", 127, False)
        environment = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
        process = await asyncio.create_subprocess_exec(
            self._git,
            *args,
            cwd=self._boundary.root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(
            self._read_limited(process.stdout, _OUTPUT_LIMIT, process)
        )
        stderr_task = asyncio.create_task(
            self._read_limited(process.stderr, _STDERR_LIMIT, process)
        )
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                (stdout, truncated), (stderr, stderr_truncated) = await asyncio.gather(
                    stdout_task,
                    stderr_task,
                )
                await process.wait()
        except TimeoutError:
            await terminate_process_tree(process)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            return _GitOutput("", "git command timed out", 124, False)
        text = stdout.decode("utf-8", errors="replace")
        if truncated:
            text += "\n[output truncated]\n"
        error_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_truncated:
            error_text += "\n[stderr truncated]"
        return _GitOutput(
            stdout=text,
            stderr=error_text,
            return_code=process.returncode or 0,
            truncated=truncated,
        )

    # 将 Git 进程结果转换为统一 ToolResult
    @staticmethod
    def _result(output: _GitOutput) -> ToolResult:
        if output.return_code != 0:
            return ToolResult(
                output.stderr or "git command failed",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(output.stdout)

    # 分派 Git 只读 action 并返回有界结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        action = params.get("action")
        payload = dict(params)
        payload.pop("action", None)
        try:
            if action == "diff":
                return await self._diff.invoke(payload)
            repository_error = await self._ensure_repository()
            if repository_error is not None:
                return repository_error
            if action == "status":
                status_request = _StatusParams.model_validate(payload)
                untracked = "all" if status_request.include_untracked else "no"
                return self._result(
                    await self._run(
                        [
                            "-c",
                            "core.quotepath=false",
                            "status",
                            "--short",
                            "--branch",
                            f"--untracked-files={untracked}",
                            "--",
                            self._path(status_request.path),
                        ]
                    )
                )
            if action == "log":
                log_request = _LogParams.model_validate(payload)
                return self._result(
                    await self._run(
                        [
                            "log",
                            f"-n{log_request.limit}",
                            "--date=iso-strict",
                            "--pretty=format:%H%x09%an%x09%ad%x09%s",
                            "--",
                            self._path(log_request.path),
                        ]
                    )
                )
            if action == "show":
                show_request = _ShowParams.model_validate(payload)
                return self._result(
                    await self._run(
                        [
                            "show",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--format=fuller",
                            "--stat",
                            "--patch",
                            show_request.revision,
                            "--",
                            self._path(show_request.path),
                        ]
                    )
                )
            if action == "blame":
                blame_request = _BlameParams.model_validate(payload)
                args = ["blame", "--line-porcelain"]
                if (
                    blame_request.start_line is not None
                    and blame_request.end_line is not None
                ):
                    args.extend(
                        (
                            "-L",
                            f"{blame_request.start_line},{blame_request.end_line}",
                        )
                    )
                args.extend(
                    (
                        blame_request.revision,
                        "--",
                        self._path(blame_request.path),
                    )
                )
                return self._result(await self._run(args))
        except ValidationError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
        return ToolResult(
            f"unknown Git action: {action}",
            is_error=True,
            error_type="schema_error",
        )


# 注册 Git family，并把 git_diff 降级为 internal/replay alias
def register_git_family(
    registry: ToolRegistry,
    boundary: WorkspaceBoundary,
    diff_backend: GitDiffTool,
    *,
    allowed_names: set[str] | None = None,
) -> GitTool | None:
    allowed = allowed_names is None or "Git" in allowed_names or "git_diff" in allowed_names
    if not allowed:
        return None
    if allowed_names is None or "git_diff" in allowed_names:
        legacy_spec = diff_backend.build_spec().model_copy(
            update={
                "model_visible": False,
                "allowed_callers": frozenset(
                    {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                ),
            }
        )
        registry.register(diff_backend, spec=legacy_spec)
    family = GitTool(boundary, diff_backend)
    if allowed_names is not None and "Git" not in allowed_names:
        spec = family.build_spec()
        diff_action = spec.action("diff")
        assert diff_action is not None
        registry.register(
            family,
            spec=spec.model_copy(
                update={
                    "actions": (diff_action,),
                    "capabilities": diff_action.capabilities,
                }
            ),
        )
    else:
        registry.register(family)
    return family

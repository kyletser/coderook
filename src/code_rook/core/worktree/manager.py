from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from code_rook.core.processes import (
    ProcessSupervisor,
    sanitized_shell_environment,
    terminate_process_tree,
)

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


# 构造不会向 Git hook、filter 或 diff driver 泄露 daemon 凭据的最小环境
def _git_environment(*, index_file: Path | None = None) -> dict[str, str]:
    env = sanitized_shell_environment()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    return env


class WorktreeError(RuntimeError):
    pass


class WorktreeApplyStateError(WorktreeError):
    pass


@dataclass(frozen=True)
class WorktreeInspection:
    name: str
    path: str
    branch: str
    base_commit: str
    changed_files: tuple[str, ...]
    diff_stat: str
    diff: str
    diff_truncated: bool


@dataclass(frozen=True)
class WorktreeApplyPreview:
    name: str
    base_commit: str
    head_commit: str
    changed_files: tuple[str, ...]
    state_digest: str
    diff: str
    diff_truncated: bool = False


@dataclass(frozen=True)
class WorktreeApplyResult:
    name: str
    base_commit: str
    changed_files: tuple[str, ...]
    state_digest: str


@dataclass(frozen=True)
class _WorktreeApplySnapshot:
    preview: WorktreeApplyPreview
    patch: bytes


class WorktreeManager:
    # 初始化受项目根目录约束的 worktree 管理器
    def __init__(
        self,
        project_root: Path,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._root = project_root.resolve()
        self._dir = self._root / ".coderook" / "worktrees"
        self._process_supervisor = process_supervisor

    # 校验名称并返回固定 worktree 路径，拒绝路径穿越
    def path_for(self, name: str) -> Path:
        if not _NAME_RE.fullmatch(name):
            raise WorktreeError("invalid worktree name")
        return self._dir / name

    # 创建独立分支和 worktree，并返回绝对路径
    async def create(self, name: str, base_ref: str = "HEAD") -> Path:
        path = self.path_for(name)
        if path.exists():
            raise WorktreeError(f"worktree already exists: {name}")
        self._dir.mkdir(parents=True, exist_ok=True)
        branch = f"coderook/{name}"
        await self._git("worktree", "add", str(path), "-b", branch, base_ref)
        return path

    # 读取指定 ref 的完整提交标识，供 Worker 固定可审查的合入基线
    async def resolve_ref(self, ref: str = "HEAD") -> str:
        return (await self._git("rev-parse", "--verify", ref)).strip()

    # 从受管 worktree 收集相对固定基线的真实状态、统计和有界 diff
    async def inspect(
        self,
        name: str,
        *,
        base_commit: str,
        max_diff_chars: int = 200_000,
    ) -> WorktreeInspection:
        path = self.path_for(name)
        if not path.is_dir():
            raise WorktreeError(f"worktree not found: {name}")
        await self._git("cat-file", "-e", f"{base_commit}^{{commit}}")
        status = await self._git(
            "-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        changed_files: list[str] = []
        entries = status.split("\x00")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            path_text = entry[3:] if len(entry) > 3 else entry
            if entry[:2] in {"R ", "C ", " R", " C"} and index < len(entries):
                renamed_to = entries[index]
                index += 1
                for renamed_path in (path_text, renamed_to):
                    if renamed_path and renamed_path not in changed_files:
                        changed_files.append(renamed_path)
                continue
            if path_text and path_text not in changed_files:
                changed_files.append(path_text)
        committed_names = await self._git(
            "-C",
            str(path),
            "diff",
            "--name-only",
            "-z",
            "--no-ext-diff",
            "--no-renames",
            base_commit,
            "--",
            ".",
        )
        for path_text in committed_names.split("\x00"):
            if path_text and path_text not in changed_files:
                changed_files.append(path_text)
        diff_stat = await self._git(
            "-C",
            str(path),
            "diff",
            "--stat",
            "--no-ext-diff",
            "--no-renames",
            base_commit,
            "--",
            ".",
        )
        raw_diff = await self._git(
            "-C",
            str(path),
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-renames",
            base_commit,
            "--",
            ".",
        )
        diff_truncated = len(raw_diff) > max_diff_chars
        diff = raw_diff[:max_diff_chars]
        branch = (await self._git("-C", str(path), "branch", "--show-current")).strip()
        return WorktreeInspection(
            name=name,
            path=str(path.resolve()),
            branch=branch,
            base_commit=base_commit,
            changed_files=tuple(changed_files),
            diff_stat=diff_stat.strip(),
            diff=diff,
            diff_truncated=diff_truncated,
        )

    # 返回绑定主仓库基线与 Worker 完整补丁的不可伪造应用摘要
    async def preview_apply(
        self,
        name: str,
        *,
        base_commit: str,
        max_diff_chars: int = 20_000,
    ) -> WorktreeApplyPreview:
        snapshot = await self._apply_snapshot(
            name,
            base_commit=base_commit,
            max_diff_chars=max_diff_chars,
        )
        return snapshot.preview

    # 重验审批摘要后先做临时索引三方检查，再把完整补丁原子应用到主工作区
    async def apply(
        self,
        name: str,
        *,
        base_commit: str,
        expected_digest: str,
        reviewed_files: tuple[str, ...],
        max_diff_chars: int = 20_000,
    ) -> WorktreeApplyResult:
        snapshot = await self._apply_snapshot(
            name,
            base_commit=base_commit,
            max_diff_chars=max_diff_chars,
        )
        preview = snapshot.preview
        if not hmac.compare_digest(preview.state_digest, expected_digest):
            raise WorktreeError("worker apply preview is stale; review the handoff again")
        if set(preview.changed_files) != set(reviewed_files):
            raise WorktreeError("worker changed files no longer match the reviewed handoff")
        with tempfile.TemporaryDirectory(prefix="coderook-worker-apply-") as temp_dir:
            patch_path = Path(temp_dir) / "handoff.patch"
            patch_path.write_bytes(snapshot.patch)
            validation_index = Path(temp_dir) / "validation.index"
            env = _git_environment(index_file=validation_index)
            await self._git_env(env, "read-tree", base_commit)
            await self._git_env(
                env,
                "apply",
                "--cached",
                "--3way",
                "--whitespace=nowarn",
                str(patch_path),
            )
            await self._git("apply", "--check", "--whitespace=nowarn", str(patch_path))
            try:
                await self._git("apply", "--whitespace=nowarn", str(patch_path))
            except WorktreeError as exc:
                if await self._workspace_status():
                    raise WorktreeApplyStateError(
                        "worker apply failed and the workspace is no longer clean"
                    ) from exc
                raise
        if (await self.resolve_ref()) != base_commit:
            raise WorktreeApplyStateError("worker apply unexpectedly changed repository HEAD")
        applied_files = tuple(await self._status_paths(self._root))
        if set(applied_files) != set(preview.changed_files):
            raise WorktreeApplyStateError(
                "worker apply postcondition failed: workspace paths differ from reviewed files"
            )
        return WorktreeApplyResult(
            name=name,
            base_commit=base_commit,
            changed_files=preview.changed_files,
            state_digest=preview.state_digest,
        )

    # 构造包含未跟踪文件的完整补丁，并把主仓库与 Worker 状态绑定到同一摘要
    async def _apply_snapshot(
        self,
        name: str,
        *,
        base_commit: str,
        max_diff_chars: int,
    ) -> _WorktreeApplySnapshot:
        inspection = await self.inspect(
            name,
            base_commit=base_commit,
            max_diff_chars=max_diff_chars + 1,
        )
        if inspection.diff_truncated or len(inspection.diff) > max_diff_chars:
            raise WorktreeError("worker inspection is truncated; apply is blocked")
        if not inspection.changed_files:
            raise WorktreeError("worker handoff has no changes to apply")
        head_commit = await self.resolve_ref()
        if head_commit != base_commit:
            raise WorktreeError("workspace HEAD moved since the worker was created")
        if await self._workspace_status():
            raise WorktreeError("workspace must be clean before applying a worker handoff")
        patch = await self._build_complete_patch(
            self.path_for(name),
            base_commit=base_commit,
        )
        if not patch:
            raise WorktreeError("worker handoff produced an empty patch")
        if len(patch) > max_diff_chars:
            raise WorktreeError("worker inspection is truncated; apply is blocked")
        digest_payload = json.dumps(
            {
                "schema": 1,
                "name": name,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "changed_files": list(inspection.changed_files),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(digest_payload + b"\x00" + patch).hexdigest()
        return _WorktreeApplySnapshot(
            preview=WorktreeApplyPreview(
                name=name,
                base_commit=base_commit,
                head_commit=head_commit,
                changed_files=inspection.changed_files,
                state_digest=digest,
                diff=patch.decode("utf-8", errors="replace"),
            ),
            patch=patch,
        )

    # 使用临时 Git index 从基线和 Worker 文件系统生成含未跟踪文件的二进制补丁
    async def _build_complete_patch(self, path: Path, *, base_commit: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="coderook-worker-index-") as temp_dir:
            env = _git_environment(index_file=Path(temp_dir) / "worker.index")
            await self._git_env(env, "-C", str(path), "read-tree", base_commit)
            await self._git_env(env, "-C", str(path), "add", "-A", "--", ".")
            return await self._git_bytes_env(
                env,
                "-C",
                str(path),
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "--no-ext-diff",
                "--no-renames",
                base_commit,
                "--",
                ".",
            )

    # 从 porcelain -z 状态读取全部受影响路径，并保留 rename 的源与目标
    async def _status_paths(self, path: Path) -> list[str]:
        output = (
            await self._workspace_status()
            if path.resolve() == self._root
            else await self._git(
                "-C",
                str(path),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
        )
        paths: list[str] = []
        entries = output.split("\x00")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            value = entry[3:] if len(entry) > 3 else entry
            if value and value not in paths:
                paths.append(value)
            if entry[:2] in {"R ", "C ", " R", " C"} and index < len(entries):
                source = entries[index]
                index += 1
                if source and source not in paths:
                    paths.append(source)
        return paths

    # 返回排除 CodeRook 自身受管 worktree 目录后的主仓库状态
    async def _workspace_status(self) -> str:
        return await self._git(
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude).coderook/worktrees",
        )

    # 列出由 CodeRook 固定目录管理的 worktree 名称和路径
    async def list(self) -> list[dict[str, str]]:
        if not self._dir.exists():
            return []
        output = await self._git("worktree", "list", "--porcelain")
        managed: list[dict[str, str]] = []
        current_path = ""
        branch = ""
        for line in (output + "\n").splitlines():
            if line.startswith("worktree "):
                current_path = line[9:]
            elif line.startswith("branch "):
                branch = line[7:]
            elif not line and current_path:
                candidate = Path(current_path).resolve()
                try:
                    name = candidate.relative_to(self._dir.resolve()).as_posix()
                except ValueError:
                    current_path, branch = "", ""
                    continue
                managed.append({"name": name, "path": str(candidate), "branch": branch})
                current_path, branch = "", ""
        return managed

    # 删除受管 worktree；默认拒绝丢弃未提交修改
    async def remove(self, name: str, discard_changes: bool = False) -> None:
        path = self.path_for(name)
        if not path.exists():
            raise WorktreeError(f"worktree not found: {name}")
        status = await self._git("-C", str(path), "status", "--porcelain")
        if status.strip() and not discard_changes:
            raise WorktreeError("worktree has uncommitted changes")
        args = ["worktree", "remove", str(path)]
        if discard_changes:
            args.append("--force")
        await self._git(*args)

    # 回滚尚未持久化为 Worker 的自动 worktree，并删除只为它创建的受管分支
    async def cleanup_failed_creation(self, name: str) -> None:
        path = self.path_for(name)
        if path.exists():
            await self._git("worktree", "remove", str(path), "--force")
        try:
            await self._git("branch", "-D", f"coderook/{name}")
        except WorktreeError as exc:
            if "not found" not in str(exc).lower():
                raise

    # 在项目仓库中运行 git 子命令，失败时转换为领域错误
    async def _git(self, *args: str) -> str:
        return (await self._git_bytes_env(_git_environment(), *args)).decode(
            errors="replace"
        )

    # 使用指定环境运行 Git 并返回解码后的标准输出
    async def _git_env(self, env: dict[str, str], *args: str) -> str:
        return (await self._git_bytes_env(env, *args)).decode(errors="replace")

    # 在项目仓库中运行 Git 并保留二进制补丁输出
    async def _git_bytes_env(
        self,
        env: dict[str, str] | None,
        *args: str,
    ) -> bytes:
        effective_env = _git_environment()
        if env is not None and env.get("GIT_INDEX_FILE"):
            effective_env["GIT_INDEX_FILE"] = env["GIT_INDEX_FILE"]
        if self._process_supervisor is not None:
            process = await self._process_supervisor.start_exec(
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(self._root),
                *args,
                label="git-worktree",
                env=effective_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-C",
                str(self._root),
                *args,
                env=effective_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if self._process_supervisor is not None:
                await self._process_supervisor.terminate(process)
            else:
                await terminate_process_tree(process)
            raise
        finally:
            if self._process_supervisor is not None and process.returncode is not None:
                self._process_supervisor.forget(process)
        output = stdout.decode(errors="replace")
        if process.returncode != 0:
            message = stderr.decode(errors="replace").strip() or output.strip()
            raise WorktreeError(message or f"git exited with {process.returncode}")
        return stdout

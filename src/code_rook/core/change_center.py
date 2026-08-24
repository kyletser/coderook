from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from code_rook.core.processes import (
    ProcessSupervisor,
    sanitized_shell_environment,
    terminate_process_tree,
)
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.workspace import WorkspaceBoundary

_OUTPUT_LIMIT = 1_000_000
_TIMEOUT_S = 30.0
_UNTRACKED_HASH_CHUNK_SIZE = 1024 * 1024
_VISIBLE_DIFF_LIMIT = 200_000


class ChangeCenterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangeCommitResult:
    commit: str
    subject: str
    files: tuple[str, ...]
    hooks_skipped: bool = True


@dataclass(frozen=True)
class _GitResult:
    code: int
    output: str
    raw_output: bytes


@dataclass(frozen=True)
class _HeadState:
    symbolic_ref: str | None
    commit: str | None


@dataclass(frozen=True)
class _RepositoryState:
    digest: str
    head: _HeadState


@dataclass(frozen=True)
class _ReviewSnapshot:
    payload: dict[str, object]
    repository: _RepositoryState


@dataclass
class _IndexLock:
    index_path: Path
    lock_path: Path
    descriptor: int
    original_snapshot: os.stat_result | None


class ChangeCenterService:
    # 绑定工作区和可选进程监管器，所有 Git mutation 都限定在该仓库
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        self._boundary = boundary
        self._git = shutil.which("git")
        self._process_supervisor = process_supervisor
        self._operation_lock = asyncio.Lock()

    # 在服务级串行区内返回与同一稳定仓库快照绑定的结构化可见 diff
    async def diff(self, scope: str = "all") -> dict[str, object]:
        async with self._operation_lock:
            return await self._diff_locked(scope)

    # 用完整状态摘要夹住可见审查，任一侧不同都拒绝返回混合快照
    async def _diff_locked(self, scope: str) -> dict[str, object]:
        return (await self._review_locked(scope)).payload

    # 生成绑定 scope、规范可见载荷和完整仓库状态的不可互换审查快照
    async def _review_locked(self, scope: str) -> _ReviewSnapshot:
        normalized_scope = self._validated_scope(scope)
        before = await self._stable_repository_state()
        result = await GitDiffTool(
            self._boundary,
            process_supervisor=self._process_supervisor,
        ).invoke(
            {
                "scope": normalized_scope,
                "path": ".",
                "diff_limit": _VISIBLE_DIFF_LIMIT,
            }
        )
        if result.is_error:
            raise ChangeCenterError(result.content)
        payload = json.loads(result.content)
        if not isinstance(payload, dict):
            raise ChangeCenterError("workspace diff returned an invalid payload")
        after = await self._stable_repository_state()
        if not hmac.compare_digest(before.digest, after.digest):
            raise ChangeCenterError(
                "workspace changed while preparing visible review; run /diff again"
            )
        payload["state_digest"] = self._review_token(
            normalized_scope,
            payload,
            before,
        )
        return _ReviewSnapshot(payload=payload, repository=before)

    # 在服务级串行区内返回经过连续一致性复核的仓库状态摘要
    async def state_digest(self) -> str:
        async with self._operation_lock:
            return (await self._stable_repository_state()).digest

    # 连续计算两次完整仓库状态以拒绝跨多条 Git 命令拼接出的竞态快照
    async def _stable_repository_state(self) -> _RepositoryState:
        first = await self._repository_state_once()
        second = await self._repository_state_once()
        if not hmac.compare_digest(first.digest, second.digest):
            raise ChangeCenterError(
                "workspace changed while computing review digest; retry review"
            )
        return second

    # 计算一次覆盖精确 HEAD 引用、index、worktree 与未跟踪内容的完整仓库状态
    async def _repository_state_once(self) -> _RepositoryState:
        head_before = await self._head_state()
        parts = [
            self._encode_head_state(head_before),
            (
                await self._run(
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                )
            ).raw_output,
            (
                await self._run("diff", "--binary", "--no-ext-diff", "--", ".")
            ).raw_output,
            (
                await self._run(
                    "diff",
                    "--cached",
                    "--binary",
                    "--no-ext-diff",
                    "--",
                    ".",
                )
            ).raw_output,
        ]
        untracked = await self._untracked_records()
        head_after = await self._head_state()
        if head_after != head_before:
            raise ChangeCenterError(
                "HEAD changed while computing review digest; retry review"
            )
        digest = hashlib.sha256()
        for part in (*parts, *untracked):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return _RepositoryState(digest=digest.hexdigest(), head=head_before)

    # 读取 HEAD 的精确符号引用与提交，兼容未出生分支并显式表示 detached HEAD
    async def _head_state(self) -> _HeadState:
        symbolic = await self._run(
            "symbolic-ref",
            "-q",
            "HEAD",
            allowed_returncodes=frozenset({0, 1}),
        )
        commit = await self._run(
            "rev-parse",
            "--verify",
            "-q",
            "HEAD^{commit}",
            allowed_returncodes=frozenset({0, 1, 128}),
        )
        symbolic_ref = symbolic.output.strip() if symbolic.code == 0 else None
        commit_sha = commit.output.strip() if commit.code == 0 else None
        if symbolic_ref is not None and (
            not symbolic_ref.startswith("refs/")
            or any(character in symbolic_ref for character in "\r\n\x00")
        ):
            raise ChangeCenterError("Git returned an invalid symbolic HEAD reference")
        if commit_sha is not None and (
            len(commit_sha) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in commit_sha)
        ):
            raise ChangeCenterError("Git returned an invalid HEAD commit")
        if symbolic_ref is None and commit_sha is None:
            raise ChangeCenterError("Git HEAD is neither a branch nor a detached commit")
        return _HeadState(symbolic_ref=symbolic_ref, commit=commit_sha)

    # 将符号引用与提交 SHA 编码为稳定长度无歧义的摘要输入
    @staticmethod
    def _encode_head_state(head: _HeadState) -> bytes:
        return json.dumps(
            {
                "symbolic_ref": head.symbolic_ref,
                "commit": head.commit,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")

    # 校验 Change Center 仅接受三个固定审查 scope
    @staticmethod
    def _validated_scope(scope: str) -> str:
        if scope not in {"all", "staged", "unstaged"}:
            raise ChangeCenterError("review scope must be all, staged, or unstaged")
        return scope

    # 对 scope、完整规范可见 payload 和仓库摘要生成不可跨操作复用的审查令牌
    @staticmethod
    def _review_token(
        scope: str,
        payload: dict[str, object],
        repository: _RepositoryState,
    ) -> str:
        canonical_payload = dict(payload)
        canonical_payload.pop("state_digest", None)
        try:
            encoded = json.dumps(
                canonical_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8", errors="surrogatepass")
        except (TypeError, ValueError) as exc:
            raise ChangeCenterError("workspace diff returned a non-canonical payload") from exc
        digest = hashlib.sha256()
        for part in (
            b"coderook-change-review-v2",
            scope.encode("ascii"),
            repository.digest.encode("ascii"),
            encoded,
        ):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.hexdigest()

    # 连续计算 worktree 对 index 的内容差异与未跟踪项摘要以保护提交窗口
    async def _stable_worktree_guard(self) -> str:
        first = await self._worktree_guard_once()
        second = await self._worktree_guard_once()
        if not hmac.compare_digest(first, second):
            raise ChangeCenterError("workspace changed while preparing commit; review again")
        return second

    # 计算一次不受 HEAD 前移影响的 worktree 与未跟踪内容摘要
    async def _worktree_guard_once(self) -> str:
        tracked = (
            await self._run("diff", "--binary", "--no-ext-diff", "--", ".")
        ).raw_output
        untracked = await self._untracked_records()
        digest = hashlib.sha256()
        for part in (tracked, *untracked):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.hexdigest()

    # 枚举并流式哈希所有未跟踪项，清单或文件竞态变化时失败关闭
    async def _untracked_records(self) -> tuple[bytes, ...]:
        before = (
            await self._run(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
            )
        ).raw_output
        raw_paths = tuple(item for item in before.split(b"\0") if item)
        records: list[bytes] = []
        for raw_path in raw_paths:
            relative = self._decode_git_path(raw_path)
            records.append(
                await asyncio.to_thread(
                    self._hash_untracked_entry,
                    relative,
                    raw_path,
                )
            )
        after = (
            await self._run(
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
            )
        ).raw_output
        if after != before:
            raise ChangeCenterError("untracked files changed while computing review digest")
        return tuple(records)

    # 将 Git 的 UTF-8/surrogateescape 路径解码为可验证的工作区相对路径
    @staticmethod
    def _decode_git_path(raw_path: bytes) -> str:
        try:
            relative = raw_path.decode("utf-8", errors="surrogateescape")
        except UnicodeError as exc:
            raise ChangeCenterError("Git returned an invalid untracked path") from exc
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(
            part in {"", ".", ".."} for part in pure.parts
        ):
            raise ChangeCenterError("Git returned an unsafe untracked path")
        return relative

    # 哈希单个未跟踪项的类型、长度与内容，拒绝穿过父级符号链接
    def _hash_untracked_entry(self, relative: str, raw_path: bytes) -> bytes:
        pure = PurePosixPath(relative)
        path = self._boundary.root.joinpath(*pure.parts)
        current = self._boundary.root
        for part in pure.parts[:-1]:
            current /= part
            try:
                parent_stat = os.lstat(current)
            except OSError as exc:
                raise ChangeCenterError(
                    f"untracked path changed while computing review digest: {relative}"
                ) from exc
            if stat.S_ISLNK(parent_stat.st_mode):
                raise ChangeCenterError(
                    f"untracked path crosses a symbolic link: {relative}"
                )
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise ChangeCenterError(
                f"untracked path changed while computing review digest: {relative}"
            ) from exc
        if stat.S_ISLNK(before.st_mode):
            return self._hash_untracked_symlink(path, relative, raw_path, before)
        if stat.S_ISREG(before.st_mode):
            return self._hash_untracked_file(path, relative, raw_path, before)
        raise ChangeCenterError(
            f"unsupported untracked file type in review digest: {relative}"
        )

    # 只哈希符号链接目标文本而不读取链接指向的工作区外内容
    @staticmethod
    def _hash_untracked_symlink(
        path: Path,
        relative: str,
        raw_path: bytes,
        before: os.stat_result,
    ) -> bytes:
        try:
            target_before = os.fsencode(os.readlink(path))
            after = os.lstat(path)
            target_after = os.fsencode(os.readlink(path))
        except OSError as exc:
            raise ChangeCenterError(
                f"untracked symlink changed while computing review digest: {relative}"
            ) from exc
        if not stat.S_ISLNK(after.st_mode) or not ChangeCenterService._same_snapshot(
            before,
            after,
        ) or target_after != target_before:
            raise ChangeCenterError(
                f"untracked symlink changed while computing review digest: {relative}"
            )
        return ChangeCenterService._encode_untracked_record(
            raw_path,
            b"symlink",
            b"120000",
            len(target_before),
            hashlib.sha256(target_before).digest(),
        )

    # 通过 no-follow 文件描述符分块哈希普通文件并在读取后复核元数据
    @staticmethod
    def _hash_untracked_file(
        path: Path,
        relative: str,
        raw_path: bytes,
        before: os.stat_result,
    ) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or not ChangeCenterService._same_identity(
                before,
                opened,
            ):
                raise ChangeCenterError(
                    f"untracked file changed while computing review digest: {relative}"
                )
            content_digest = hashlib.sha256()
            bytes_read = 0
            while chunk := os.read(descriptor, _UNTRACKED_HASH_CHUNK_SIZE):
                content_digest.update(chunk)
                bytes_read += len(chunk)
            finished = os.fstat(descriptor)
            after = os.lstat(path)
        except ChangeCenterError:
            raise
        except OSError as exc:
            raise ChangeCenterError(
                f"untracked file changed while computing review digest: {relative}"
            ) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if bytes_read != finished.st_size or not ChangeCenterService._same_snapshot(
            opened,
            finished,
        ) or not (
            stat.S_ISREG(after.st_mode)
            and ChangeCenterService._same_identity(finished, after)
            and ChangeCenterService._same_snapshot(before, after)
        ):
            raise ChangeCenterError(
                f"untracked file changed while computing review digest: {relative}"
            )
        return ChangeCenterService._encode_untracked_record(
            raw_path,
            b"regular",
            (
                b"100755"
                if finished.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                else b"100644"
            ),
            int(finished.st_size),
            content_digest.digest(),
        )

    # 比较设备、inode 和类型以识别路径在打开前后是否被替换
    @staticmethod
    def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
            and left.st_dev == right.st_dev
            and left.st_ino == right.st_ino
        )

    # 在同种 stat 来源间比较身份、长度和纳秒时间以检测读取期间写入
    @staticmethod
    def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
        return (
            ChangeCenterService._same_identity(left, right)
            and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
            and left.st_size == right.st_size
            and left.st_mtime_ns == right.st_mtime_ns
            and left.st_ctime_ns == right.st_ctime_ns
        )

    # 以显式长度帧编码未跟踪路径、类型、可提交 Git mode、长度与内容哈希
    @staticmethod
    def _encode_untracked_record(
        raw_path: bytes,
        kind: bytes,
        git_mode: bytes,
        length: int,
        content_digest: bytes,
    ) -> bytes:
        return b"".join(
            (
                len(raw_path).to_bytes(8, "big"),
                raw_path,
                len(kind).to_bytes(2, "big"),
                kind,
                len(git_mode).to_bytes(2, "big"),
                git_mode,
                length.to_bytes(8, "big"),
                content_digest,
            )
        )

    # 校验并归一化用户返回的审查摘要，拒绝空值和非 SHA-256 形态
    @staticmethod
    def _validated_digest(expected_digest: str) -> str:
        expected = expected_digest.strip().casefold()
        if len(expected) != 64 or any(
            character not in "0123456789abcdef" for character in expected
        ):
            raise ChangeCenterError("a valid reviewed state digest is required")
        return expected

    # 将当前可见 payload 与用户审查摘要逐字节绑定，状态不同立即失败关闭
    @staticmethod
    def _require_reviewed_payload(
        payload: dict[str, object],
        expected_digest: str,
        *,
        required_scope: str,
    ) -> str:
        expected = ChangeCenterService._validated_digest(expected_digest)
        actual = payload.get("state_digest")
        if payload.get("scope") != required_scope:
            raise ChangeCenterError(
                f"a {required_scope} review is required for this action"
            )
        if not isinstance(actual, str) or not hmac.compare_digest(actual, expected):
            raise ChangeCenterError("workspace changed after review; run /diff and confirm again")
        return expected

    # 在任何 Git mutation 前重新计算稳定摘要并确认仍等于用户已审查状态
    async def _require_digest(self, expected_digest: str) -> None:
        expected = self._validated_digest(expected_digest)
        actual = (await self._stable_repository_state()).digest
        if not hmac.compare_digest(actual, expected):
            raise ChangeCenterError("workspace changed after review; run /diff and confirm again")

    # 要求每个待写入路径都具有完整可见文本补丁或完整二进制摘要
    @staticmethod
    def _require_visible_review(
        payload: dict[str, object],
        paths: tuple[str, ...],
    ) -> None:
        records = {
            str(item.get("path", "")): item
            for item in ChangeCenterService._file_records(payload)
            if item.get("path")
        }
        missing = [path for path in paths if path not in records]
        if missing:
            raise ChangeCenterError(
                "selected paths are not current workspace changes: " + ", ".join(missing)
            )
        blocked = [
            path
            for path in paths
            if records[path].get("review_complete") is not True
        ]
        if blocked:
            raise ChangeCenterError(
                "complete visible review is required before stage or commit: "
                + ", ".join(blocked)
            )

    # 串行执行 stage 事务，确保同一服务内的审查和 Git mutation 不会交错
    async def stage(
        self,
        paths: list[str],
        *,
        expected_digest: str,
    ) -> dict[str, object]:
        async with self._operation_lock:
            return await self._stage_locked(paths, expected_digest=expected_digest)

    # 在临时 index 中固化已审查内容，最终复核后才原子替换真实 index
    async def _stage_locked(
        self,
        paths: list[str],
        *,
        expected_digest: str,
    ) -> dict[str, object]:
        normalized = tuple(dict.fromkeys(self._normalize_path(path) for path in paths))
        if not normalized:
            raise ChangeCenterError("at least one changed file must be selected")
        review = await self._review_locked("all")
        current = review.payload
        self._require_reviewed_payload(
            current,
            expected_digest,
            required_scope="all",
        )
        await self._reject_conflicts()
        await self._reject_outside_staged_changes()
        self._require_visible_review(current, normalized)
        excluded_index_paths = await self._selected_repository_paths(current, normalized)
        index_lock = await self._acquire_index_lock()
        temporary_index: Path | None = None
        try:
            temporary_index = await self._prepare_staged_index(
                normalized,
                review.repository.digest,
                index_lock,
                excluded_index_paths,
            )
            await self._publish_staged_index(
                temporary_index,
                review.repository.digest,
                index_lock,
            )
        finally:
            if temporary_index is not None:
                self._discard_temporary_index(temporary_index)
            self._release_index_lock(index_lock)
        return await self._diff_locked("staged")

    # 串行创建只含已审查 staged tree 的本地提交
    async def commit(
        self,
        message: str,
        *,
        expected_digest: str,
    ) -> ChangeCommitResult:
        subject = " ".join(message.split())
        if not subject or len(subject) > 200:
            raise ChangeCenterError("commit subject must contain 1-200 characters")
        async with self._operation_lock:
            return await self._commit_locked(subject, expected_digest=expected_digest)

    # 用 commit-tree 和 compare-and-swap update-ref 提交确切审查树而不重新读取工作区
    async def _commit_locked(
        self,
        subject: str,
        *,
        expected_digest: str,
    ) -> ChangeCommitResult:
        review = await self._review_locked("staged")
        staged = review.payload
        self._require_reviewed_payload(
            staged,
            expected_digest,
            required_scope="staged",
        )
        await self._reject_conflicts()
        await self._reject_outside_staged_changes()
        files = tuple(
            str(item.get("path", ""))
            for item in self._file_records(staged)
            if item.get("path")
        )
        if not files:
            raise ChangeCenterError("no staged changes to commit")
        self._require_visible_review(staged, files)
        head = review.repository.head
        if head.symbolic_ref is None:
            raise ChangeCenterError(
                "detached HEAD cannot be committed from Change Center; switch to a branch"
            )
        await self._run("diff", "--cached", "--check", "--", ".")
        index_lock = await self._acquire_index_lock()
        update_attempted = False
        commit = ""
        committed_files: tuple[str, ...] = ()
        temporary_index: Path | None = None
        try:
            await self._require_digest(review.repository.digest)
            self._require_original_index(index_lock)
            if await self._head_state() != head:
                raise ChangeCenterError("HEAD changed after review; run /diff and confirm again")
            temporary_index = await self._snapshot_index(index_lock)
            tree = (
                await self._run(
                    "write-tree",
                    environment_overrides={
                        "GIT_INDEX_FILE": str(temporary_index.absolute())
                    },
                )
            ).output.strip()
            worktree_guard = await self._stable_worktree_guard()
            commit_args = ["commit-tree", tree]
            if head.commit is not None:
                commit_args.extend(("-p", head.commit))
            commit_args.extend(("-m", subject))
            commit = (await self._run(*commit_args)).output.strip()
            old_value = head.commit or ("0" * len(commit))
            update_attempted = True
            await self._run("update-ref", head.symbolic_ref, commit, old_value)
            if await self._head_state() != _HeadState(head.symbolic_ref, commit):
                raise ChangeCenterError(
                    "HEAD changed during commit; the reviewed commit will be rolled back"
                )
            after_guard = await self._stable_worktree_guard()
            if not hmac.compare_digest(worktree_guard, after_guard):
                raise ChangeCenterError(
                    "workspace changed during commit; the reviewed commit was rolled back"
                )
            committed_files = await self._committed_files(commit)
            if await self._head_state() != _HeadState(head.symbolic_ref, commit):
                raise ChangeCenterError(
                    "HEAD changed before commit confirmation; refusing a stale success"
                )
        except BaseException:
            if update_attempted and commit:
                try:
                    await self._recover_reference_safely(head, commit)
                except BaseException as rollback_error:
                    raise ChangeCenterError(
                        "commit reference outcome is unsafe and automatic rollback failed"
                    ) from rollback_error
            raise
        finally:
            if temporary_index is not None:
                self._discard_temporary_index(temporary_index)
            self._release_index_lock(index_lock)
        return ChangeCommitResult(
            commit=commit,
            subject=subject,
            files=committed_files,
        )

    # 从真实 index 安全字节快照构造临时 index 并保护所有未选择条目的完整语义
    async def _prepare_staged_index(
        self,
        paths: tuple[str, ...],
        expected_digest: str,
        index_lock: _IndexLock,
        excluded_index_paths: frozenset[bytes],
    ) -> Path:
        before_semantics = await self._index_semantics()
        temporary = await self._snapshot_index(index_lock)
        environment = {"GIT_INDEX_FILE": str(temporary.absolute())}
        try:
            await self._run(
                "add",
                "--",
                *paths,
                environment_overrides=environment,
            )
            remaining = await self._run(
                "diff",
                "--name-only",
                "-z",
                "--",
                *paths,
                environment_overrides=environment,
            )
            if remaining.raw_output:
                raise ChangeCenterError(
                    "selected files changed while preparing stage; review again"
                )
            after_semantics = await self._index_semantics(environment)
            self._require_preserved_index_semantics(
                before_semantics,
                after_semantics,
                excluded_index_paths,
            )
            await self._require_digest(expected_digest)
            self._require_original_index(index_lock)
        except BaseException:
            self._discard_temporary_index(temporary)
            raise
        return temporary

    # 只删除本次私有临时 index 与其锁文件，不触碰 Git 元数据目录中的其他文件
    @staticmethod
    def _discard_temporary_index(temporary: Path) -> None:
        for candidate in (temporary, temporary.with_name(f"{temporary.name}.lock")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    # 在已持有的真实 index lock 下最后复核仓库并原子发布临时 index
    async def _publish_staged_index(
        self,
        temporary: Path,
        expected_digest: str,
        index_lock: _IndexLock,
    ) -> None:
        await self._require_digest(expected_digest)
        self._require_original_index(index_lock)
        self._copy_index_to_lock(temporary, index_lock)
        os.close(index_lock.descriptor)
        index_lock.descriptor = -1
        os.replace(index_lock.lock_path, index_lock.index_path)

    # 在真实 index 所在文件系统中创建安全字节快照，未出生仓库则创建标准空 index
    async def _snapshot_index(self, index_lock: _IndexLock) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="coderook-index-",
            dir=index_lock.index_path.parent,
        )
        temporary = Path(raw_path)
        source = -1
        try:
            if index_lock.original_snapshot is None:
                os.close(descriptor)
                descriptor = -1
                temporary.unlink()
                await self._run(
                    "read-tree",
                    "--empty",
                    environment_overrides={"GIT_INDEX_FILE": str(temporary.absolute())},
                )
                return temporary
            flags = (
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            source = os.open(index_lock.index_path, flags)
            opened = os.fstat(source)
            if not self._same_opened_snapshot(index_lock.original_snapshot, opened):
                raise ChangeCenterError(
                    "Git index changed while creating its safe snapshot; review again"
                )
            while chunk := os.read(source, 1024 * 1024):
                offset = 0
                while offset < len(chunk):
                    offset += os.write(descriptor, chunk[offset:])
            os.fsync(descriptor)
            finished = os.fstat(source)
            self._require_original_index(index_lock)
            if not self._same_snapshot(opened, finished):
                raise ChangeCenterError(
                    "Git index changed while creating its safe snapshot; review again"
                )
            return temporary
        except BaseException:
            self._discard_temporary_index(temporary)
            raise
        finally:
            if source >= 0:
                os.close(source)
            if descriptor >= 0:
                os.close(descriptor)

    # 比较路径 stat 与已打开描述符的身份、长度和 mtime，规避 Windows ctime 口径差异
    @staticmethod
    def _same_opened_snapshot(
        path_stat: os.stat_result,
        opened_stat: os.stat_result,
    ) -> bool:
        return (
            ChangeCenterService._same_identity(path_stat, opened_stat)
            and path_stat.st_size == opened_stat.st_size
            and path_stat.st_mtime_ns == opened_stat.st_mtime_ns
        )

    # 读取包含 mode、OID、stage 与 index 标志标签的逐路径语义记录
    async def _index_semantics(
        self,
        environment_overrides: dict[str, str] | None = None,
    ) -> dict[bytes, bytes]:
        raw = (
            await self._run(
                "ls-files",
                "--full-name",
                "--stage",
                "-v",
                "-f",
                "-z",
                "--",
                environment_overrides=environment_overrides,
            )
        ).raw_output
        records: dict[bytes, bytes] = {}
        for record in (item for item in raw.split(b"\0") if item):
            separator = record.find(b"\t")
            if separator <= 0 or separator == len(record) - 1:
                raise ChangeCenterError("Git returned an invalid index semantics record")
            path = record[separator + 1 :]
            if path in records:
                raise ChangeCenterError(
                    "Git index contains unresolved multi-stage entries; resolve conflicts"
                )
            records[path] = record[:separator]
        return records

    # 确认临时 add 没有改变任何未选择条目的内容或特殊 index 标志
    @staticmethod
    def _require_preserved_index_semantics(
        before: dict[bytes, bytes],
        after: dict[bytes, bytes],
        excluded_paths: frozenset[bytes],
    ) -> None:
        protected_before = {
            path: record for path, record in before.items() if path not in excluded_paths
        }
        protected_after = {
            path: record for path, record in after.items() if path not in excluded_paths
        }
        if protected_before != protected_after:
            raise ChangeCenterError(
                "Git could not preserve sparse, split, skip-worktree, or "
                "assume-unchanged index semantics safely"
            )

    # 把所选文件及 rename 原路径转换为仓库根相对原始字节名以限定 index 变化
    async def _selected_repository_paths(
        self,
        payload: dict[str, object],
        paths: tuple[str, ...],
    ) -> frozenset[bytes]:
        selected = set(paths)
        names: set[str] = set(paths)
        for record in self._file_records(payload):
            if str(record.get("path", "")) not in selected:
                continue
            original = record.get("original_path")
            if isinstance(original, str) and original:
                names.add(original)
        prefix = await self._repository_prefix()
        encoded: set[bytes] = set()
        for name in names:
            lexical = self._validated_lexical_path(name)
            repository_name = f"{prefix}/{lexical}" if prefix else lexical
            encoded.add(os.fsencode(repository_name))
        return frozenset(encoded)

    # 返回规范仓库根到当前工作区的词法前缀并拒绝工作区不在仓库内的状态
    async def _repository_prefix(self) -> str:
        raw = (await self._run("rev-parse", "--show-toplevel")).output.strip()
        if not raw or "\x00" in raw:
            raise ChangeCenterError("Git returned an invalid repository root")
        try:
            repository = Path(raw).resolve(strict=True)
            relative = self._boundary.root.relative_to(repository)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ChangeCenterError(
                "workspace must be contained by its Git repository root"
            ) from exc
        return "" if str(relative) == "." else relative.as_posix()

    # 拒绝子目录工作区之外的任何 staged 路径，防止不可见内容进入提交树
    async def _reject_outside_staged_changes(self) -> None:
        prefix = await self._repository_prefix()
        if not prefix:
            return
        raw = (
            await self._run(
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--",
            )
        ).raw_output
        prefix_bytes = os.fsencode(prefix) + b"/"
        outside = [
            path
            for path in raw.split(b"\0")
            if path and not path.startswith(prefix_bytes)
        ]
        if outside:
            names = ", ".join(
                os.fsdecode(path) for path in outside[:10]
            )
            raise ChangeCenterError(
                "staged changes outside this workspace must be cleared first: " + names
            )

    # 从新提交对象读取真实变更路径并再次确认没有越出子目录工作区
    async def _committed_files(self, commit: str) -> tuple[str, ...]:
        raw = (
            await self._run(
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit,
            )
        ).raw_output
        prefix = await self._repository_prefix()
        prefix_bytes = os.fsencode(prefix) + b"/" if prefix else b""
        files: list[str] = []
        for path in (item for item in raw.split(b"\0") if item):
            if prefix_bytes and not path.startswith(prefix_bytes):
                raise ChangeCenterError(
                    "created commit contains changes outside the current workspace"
                )
            relative = path[len(prefix_bytes) :] if prefix_bytes else path
            files.append(os.fsdecode(relative))
        return tuple(dict.fromkeys(files))

    # 屏蔽调用方取消直至引用探测和必要的 CAS 回滚确定完成
    async def _recover_reference_safely(
        self,
        original: _HeadState,
        attempted_commit: str,
    ) -> None:
        recovery = asyncio.create_task(
            self._recover_reference(original, attempted_commit)
        )
        while not recovery.done():
            try:
                await asyncio.shield(recovery)
            except asyncio.CancelledError:
                continue
        recovery.result()

    # 探测 update-ref 的真实结果，仅在引用仍指向本次提交时执行精确 CAS 回滚
    async def _recover_reference(
        self,
        original: _HeadState,
        attempted_commit: str,
    ) -> None:
        if original.symbolic_ref is None:
            raise ChangeCenterError("detached HEAD has no safe rollback reference")
        current = await self._head_state()
        if current == original:
            return
        if (
            current.symbolic_ref != original.symbolic_ref
            or current.commit != attempted_commit
        ):
            raise ChangeCenterError(
                "HEAD moved to an unrelated reference state; refusing unsafe rollback"
            )
        if original.commit is None:
            await self._run(
                "update-ref",
                "-d",
                original.symbolic_ref,
                attempted_commit,
            )
        else:
            await self._run(
                "update-ref",
                original.symbolic_ref,
                original.commit,
                attempted_commit,
            )
        if await self._head_state() != original:
            raise ChangeCenterError("HEAD rollback did not restore the reviewed reference")

    # 解析并验证 Git 当前 index 路径不是符号链接或非普通文件
    async def _index_path(self) -> Path:
        raw = (await self._run("rev-parse", "--git-path", "index")).output.strip()
        if not raw or "\x00" in raw:
            raise ChangeCenterError("Git returned an invalid index path")
        reported = Path(raw)
        index_path = (
            reported if reported.is_absolute() else self._boundary.root / reported
        ).absolute()
        parent = index_path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise ChangeCenterError("Git index parent directory is unsafe")
        if os.path.lexists(index_path) and (
            index_path.is_symlink() or not index_path.is_file()
        ):
            raise ChangeCenterError("Git index path is unsafe")
        return index_path

    # 独占创建标准 Git index.lock 并记录真实 index 的原始身份
    async def _acquire_index_lock(self) -> _IndexLock:
        index_path = await self._index_path()
        original = os.lstat(index_path) if os.path.lexists(index_path) else None
        lock_path = index_path.with_name(f"{index_path.name}.lock")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except FileExistsError as exc:
            raise ChangeCenterError(
                "Git index is locked by another operation; retry review"
            ) from exc
        except OSError as exc:
            raise ChangeCenterError("Git index lock could not be acquired safely") from exc
        index_lock = _IndexLock(
            index_path=index_path,
            lock_path=lock_path,
            descriptor=descriptor,
            original_snapshot=original,
        )
        try:
            self._require_original_index(index_lock)
        except BaseException:
            self._release_index_lock(index_lock)
            raise
        return index_lock

    # 比较 index 在加锁前后的身份、长度和时间，拒绝覆盖并发更新
    @staticmethod
    def _require_original_index(index_lock: _IndexLock) -> None:
        exists = os.path.lexists(index_lock.index_path)
        if index_lock.original_snapshot is None:
            if exists:
                raise ChangeCenterError(
                    "Git index changed after review; run /diff and confirm again"
                )
            return
        if not exists:
            raise ChangeCenterError(
                "Git index changed after review; run /diff and confirm again"
            )
        current = os.lstat(index_lock.index_path)
        if not ChangeCenterService._same_snapshot(
            index_lock.original_snapshot,
            current,
        ):
            raise ChangeCenterError(
                "Git index changed after review; run /diff and confirm again"
            )

    # 将临时 index 的确切字节写入已独占创建的 lock 文件并刷盘
    @staticmethod
    def _copy_index_to_lock(temporary: Path, index_lock: _IndexLock) -> None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        source = -1
        try:
            source = os.open(temporary, flags)
            source_stat = os.fstat(source)
            if not stat.S_ISREG(source_stat.st_mode):
                raise ChangeCenterError("temporary Git index is unsafe")
            while chunk := os.read(source, 1024 * 1024):
                offset = 0
                while offset < len(chunk):
                    offset += os.write(index_lock.descriptor, chunk[offset:])
            os.fsync(index_lock.descriptor)
        except ChangeCenterError:
            raise
        except OSError as exc:
            raise ChangeCenterError("temporary Git index could not be published safely") from exc
        finally:
            if source >= 0:
                os.close(source)

    # 关闭并删除尚未发布的 Git index lock，不触碰真实 index
    @staticmethod
    def _release_index_lock(index_lock: _IndexLock) -> None:
        if index_lock.descriptor >= 0:
            os.close(index_lock.descriptor)
            index_lock.descriptor = -1
        try:
            index_lock.lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    # 从不可信 diff payload 提取字典文件记录，其他形态一律按空集合处理
    @staticmethod
    def _file_records(payload: dict[str, object]) -> list[dict[str, object]]:
        raw = payload.get("files")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    # 校验词法相对路径并原样保留路径名称，不解析最终符号链接目标
    @staticmethod
    def _validated_lexical_path(value: str) -> str:
        if (
            not value
            or any(character in value for character in "\x00\r\n")
            or Path(value).is_absolute()
        ):
            raise ChangeCenterError("stage paths must be workspace-relative files")
        if os.name != "nt" and "\\" in value:
            raise ChangeCenterError(
                "backslash file names cannot be represented safely by Git review payloads"
            )
        lexical = value.replace("\\", "/") if os.name == "nt" else value
        parts = lexical.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ChangeCenterError(
                "stage path is outside the workspace or not a safe lexical file name"
            )
        if parts[0].casefold() == ".git":
            raise ChangeCenterError("repository metadata cannot be staged")
        return "/".join(parts)

    # 将用户路径规范为词法 POSIX 名称并仅解析父目录以阻断越界链接
    def _normalize_path(self, value: str) -> str:
        relative = self._validated_lexical_path(value)
        pure = PurePosixPath(relative)
        try:
            parent = self._boundary.root.joinpath(*pure.parts[:-1]).resolve(strict=False)
            parent.relative_to(self._boundary.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ChangeCenterError("stage path parent escapes the workspace") from exc
        return relative

    # 检查未解决 merge 条目，任何冲突都阻断 stage 和 commit
    async def _reject_conflicts(self) -> None:
        conflicts = (await self._run("ls-files", "-u", "-z")).output
        if conflicts:
            raise ChangeCenterError(
                "unresolved Git conflicts must be resolved before Change Center actions"
            )

    # 从合并输出流读取有界字节，超过上限时让调用方终止整个进程树
    async def _read_output(self, process: asyncio.subprocess.Process) -> tuple[bytes, bool]:
        if process.stdout is None:
            return b"", False
        output = bytearray()
        while chunk := await process.stdout.read(64 * 1024):
            room = _OUTPUT_LIMIT - len(output)
            if room <= 0:
                return bytes(output), True
            output.extend(chunk[:room])
            if len(chunk) > room:
                return bytes(output), True
        return bytes(output), False

    # 执行单个有界 Git 命令并允许为临时 index 追加非敏感环境覆盖
    async def _run(
        self,
        *args: str,
        environment_overrides: dict[str, str] | None = None,
        allowed_returncodes: frozenset[int] = frozenset({0}),
    ) -> _GitResult:
        if self._git is None:
            raise ChangeCenterError("git executable is unavailable")
        command = (
            self._git,
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-C",
            str(self._boundary.root),
            *args,
        )
        environment = sanitized_shell_environment()
        environment.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PAGER": "cat",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        if environment_overrides:
            environment.update(environment_overrides)
        if self._process_supervisor is not None:
            process = await self._process_supervisor.start_exec(
                *command,
                label="change-center-git",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *command,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        try:
            output, exceeded = await asyncio.wait_for(
                self._read_output(process),
                timeout=_TIMEOUT_S,
            )
            if exceeded:
                await self._terminate(process)
                raise ChangeCenterError("git output exceeded the safety limit")
            await asyncio.wait_for(process.wait(), timeout=_TIMEOUT_S)
        except TimeoutError:
            await self._terminate(process)
            raise ChangeCenterError("git command timed out") from None
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate(process))
            raise
        finally:
            if self._process_supervisor is not None and process.returncode is not None:
                self._process_supervisor.forget(process)
        text = output.decode("utf-8", errors="replace")
        return_code = int(process.returncode or 0)
        if return_code not in allowed_returncodes:
            raise ChangeCenterError(
                text.strip()[:10_000] or f"git exited with {process.returncode}"
            )
        return _GitResult(
            code=return_code,
            output=text,
            raw_output=output,
        )

    # 通过共享监管器或平台原生实现终止 Git 进程树
    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if self._process_supervisor is not None:
            await self._process_supervisor.terminate(process)
        else:
            await terminate_process_tree(process)

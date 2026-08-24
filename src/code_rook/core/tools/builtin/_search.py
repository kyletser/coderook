from __future__ import annotations

import asyncio
import base64
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

from pathspec import PathSpec
from pathspec.gitignore import GitIgnoreSpec

from code_rook.core.processes import (
    ProcessSupervisor,
    sanitized_shell_environment,
    terminate_process_tree,
)
from code_rook.core.workspace import WorkspaceBoundary, WorkspaceBoundaryError

DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git/",
    ".hg/",
    ".svn/",
    ".mypy_cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "node_modules/",
    "dist/",
    "build/",
    "*.pyc",
)

MAX_SEARCH_FILE_BYTES = 1 * 1024 * 1024


class SearchPathFilter:
    def __init__(
        self,
        workspace_root: Path,
        *,
        file_glob: str | None,
        include_hidden: bool,
        search_root: Path,
    ) -> None:
        self._workspace_root = workspace_root
        self._search_root = search_root
        self._include_hidden = include_hidden
        self._ignore_spec = _load_ignore_spec(workspace_root)
        self._include_spec = (
            PathSpec.from_lines("gitignore", [validate_glob_pattern(file_glob)])
            if file_glob is not None
            else None
        )

    def allows(self, relative_path: str) -> bool:
        normalized = relative_path.replace("\\", "/")
        if not self._include_hidden and _is_hidden(normalized):
            return False
        if self._ignore_spec.match_file(normalized):
            return False
        full_path = self._workspace_root / Path(normalized)
        try:
            full_path.resolve().relative_to(self._workspace_root)
        except (OSError, ValueError):
            return False
        if self._include_spec is None:
            return True
        try:
            search_relative = full_path.relative_to(self._search_root).as_posix()
        except ValueError:
            return False
        return self._include_spec.match_file(normalized) or self._include_spec.match_file(
            search_relative
        )


def resolve_search_root(boundary: WorkspaceBoundary, value: str) -> tuple[Path, str]:
    root = boundary.resolve(value)
    if not root.exists():
        raise FileNotFoundError(f"search path does not exist: {value}")
    relative = root.relative_to(boundary.root).as_posix()
    return root, relative or "."


def validate_glob_pattern(pattern: str) -> str:
    normalized = pattern.replace("\\", "/")
    if not normalized or "\x00" in normalized:
        raise ValueError("glob pattern must be non-empty")
    if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
        raise ValueError("glob pattern must stay within the search path")
    if "/../" in normalized or normalized.endswith("/.."):
        raise ValueError("glob pattern must not contain parent traversal")
    return normalized


def ripgrep_path() -> str | None:
    return shutil.which("rg")


def ripgrep_ignore_args(workspace_root: Path) -> list[str]:
    args: list[str] = []
    for pattern in DEFAULT_EXCLUDES:
        args.extend(("--glob", f"!{pattern}"))
    for name in (".gitignore", ".ignore"):
        if (workspace_root / name).is_file():
            args.extend(("--ignore-file", name))
    return args


async def start_process(
    executable: str,
    args: list[str],
    *,
    cwd: Path,
    process_supervisor: ProcessSupervisor | None = None,
) -> asyncio.subprocess.Process:
    if process_supervisor is not None:
        return await process_supervisor.start_exec(
            executable,
            *args,
            label="workspace-search",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    process = await asyncio.create_subprocess_exec(
        executable,
        *args,
        cwd=cwd,
        env=sanitized_shell_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return process


async def stop_process(
    process: asyncio.subprocess.Process,
    process_supervisor: ProcessSupervisor | None = None,
) -> None:
    if process_supervisor is not None:
        await process_supervisor.terminate(process)
        return
    if process.returncode is None:
        await terminate_process_tree(process)
    await process.communicate()


# 从统一监管表移除已自然结束且完成回收的搜索进程
def forget_process(
    process: asyncio.subprocess.Process,
    process_supervisor: ProcessSupervisor | None,
) -> None:
    if process_supervisor is not None:
        process_supervisor.forget(process)


def decode_rg_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    text = value.get("text")
    if isinstance(text, str):
        return text
    encoded = value.get("bytes")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded).decode("utf-8", errors="replace")
        except ValueError:
            return ""
    return ""


def _load_ignore_spec(workspace_root: Path) -> GitIgnoreSpec:
    patterns = list(DEFAULT_EXCLUDES)
    for name in (".gitignore", ".ignore"):
        path = workspace_root / name
        if path.is_file():
            patterns.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return GitIgnoreSpec.from_lines(patterns)


def _is_hidden(relative_path: str) -> bool:
    return any(part.startswith(".") for part in Path(relative_path).parts)


def iter_workspace_files(
    boundary: WorkspaceBoundary,
    search_root: Path,
    *,
    file_glob: str | None = None,
    include_hidden: bool = False,
) -> Iterator[tuple[Path, str]]:
    path_filter = SearchPathFilter(
        boundary.root,
        file_glob=file_glob,
        include_hidden=include_hidden,
        search_root=search_root,
    )
    ignore_spec = path_filter._ignore_spec

    if search_root.is_file():
        candidates: Iterator[Path] = iter((search_root,))
    else:
        candidates = _walk_files(boundary, search_root, ignore_spec, include_hidden)

    for candidate in candidates:
        try:
            boundary.resolve(str(candidate))
        except WorkspaceBoundaryError:
            continue
        relative = candidate.relative_to(boundary.root).as_posix()
        if not path_filter.allows(relative):
            continue
        yield candidate, relative


def _walk_files(
    boundary: WorkspaceBoundary,
    search_root: Path,
    ignore_spec: GitIgnoreSpec,
    include_hidden: bool,
) -> Iterator[Path]:
    for dir_path, dir_names, file_names in os.walk(search_root, followlinks=False):
        directory = Path(dir_path)
        kept_dirs: list[str] = []
        for name in sorted(dir_names, key=str.casefold):
            candidate = directory / name
            relative = candidate.relative_to(boundary.root).as_posix()
            if candidate.is_symlink():
                continue
            if not include_hidden and _is_hidden(relative):
                continue
            if ignore_spec.match_file(relative + "/"):
                continue
            kept_dirs.append(name)
        dir_names[:] = kept_dirs

        for name in sorted(file_names, key=str.casefold):
            yield directory / name


def read_search_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_SEARCH_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")

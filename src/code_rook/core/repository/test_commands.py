from __future__ import annotations

import hashlib
import json
import os
import shlex
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from code_rook.core.tools.builtin._search import iter_workspace_files, read_search_text
from code_rook.core.workspace import WorkspaceBoundary

_MANIFEST_NAMES = frozenset(
    {
        "build.gradle",
        "build.gradle.kts",
        "cargo.toml",
        "gemfile",
        "go.mod",
        "package.json",
        "pom.xml",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
    }
)
_NODE_MANAGERS = frozenset({"bun", "npm", "pnpm", "yarn"})
_DOTNET_SUFFIXES = frozenset({".csproj", ".fsproj", ".sln"})
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class TestCommandCandidate:
    ecosystem: str
    argv: tuple[str, ...]
    cwd: str
    source: str
    reason: str
    trust: str = "manifest_declared"
    source_digest: str = ""


@dataclass(frozen=True)
class TestCommandDiscovery:
    candidates: tuple[TestCommandCandidate, ...]
    scanned_manifests: int
    truncated: bool


# 用 cmd.exe 可安全解释的强制双引号包装参数并拒绝会发生变量展开的字符
def _quote_windows_shell_argument(value: str) -> str:
    if any(character in value for character in ('"', "%", "!", "\r", "\n", "\x00")):
        raise ValueError("test command candidate is unsafe for the Windows shell")
    return f'"{value}"'


# 把固定 argv 与候选工作目录渲染为可精确审查且跨平台的 shell 命令
def render_test_command(candidate: TestCommandCandidate) -> str:
    command = (
        " ".join(_quote_windows_shell_argument(item) for item in candidate.argv)
        if os.name == "nt"
        else shlex.join(candidate.argv)
    )
    if candidate.cwd == ".":
        return command
    if os.name == "nt":
        directory = _quote_windows_shell_argument(candidate.cwd)
        return f"cd /d {directory} && {command}"
    return f"cd {shlex.quote(candidate.cwd)} && {command}"


# 从候选来源、目录与 argv 生成稳定标识，避免模型把任意命令冒充发现结果
def command_candidate_id(candidate: TestCommandCandidate) -> str:
    payload = json.dumps(
        {
            "schema": 1,
            "source": candidate.source,
            "cwd": candidate.cwd,
            "argv": list(candidate.argv),
            "trust": candidate.trust,
            "source_digest": candidate.source_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# 有界哈希 manifest 原始字节并在读取前后复核元数据，变化或超限时失败关闭
def _manifest_digest(path: Path) -> str | None:
    try:
        before = path.stat()
        if before.st_size > _MAX_MANIFEST_BYTES:
            return None
        digest = hashlib.sha256()
        read_bytes = 0
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_BYTES):
                read_bytes += len(chunk)
                if read_bytes > _MAX_MANIFEST_BYTES:
                    return None
                digest.update(chunk)
        after = path.stat()
    except OSError:
        return None
    stable_fields = ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if read_bytes != before.st_size or any(
        getattr(before, field, None) != getattr(after, field, None)
        for field in stable_fields
    ):
        return None
    return digest.hexdigest()


# 把目录转换为稳定工作区相对路径，仓库根目录使用点号
def _relative_directory(boundary: WorkspaceBoundary, directory: Path) -> str:
    relative = directory.relative_to(boundary.root).as_posix()
    return relative or "."


# 递归检查 TOML 值是否声明 pytest 依赖，不执行任何 manifest 内容
def _contains_pytest(value: object) -> bool:
    if isinstance(value, str):
        return "pytest" in value.casefold()
    if isinstance(value, list):
        return any(_contains_pytest(item) for item in value)
    if isinstance(value, dict):
        return any(
            "pytest" in str(key).casefold() or _contains_pytest(item) for key, item in value.items()
        )
    return False


# 根据可信 lockfile 和 packageManager 字段选择 Node 包管理器名称
def _node_manager(directory: Path, payload: dict[str, Any]) -> str:
    declared = payload.get("packageManager")
    if isinstance(declared, str):
        name = declared.partition("@")[0].casefold()
        if name in _NODE_MANAGERS:
            return name
    for filename, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
        ("bun.lockb", "bun"),
    ):
        if (directory / filename).is_file():
            return manager
    return "npm"


# 从 Python 项目元数据生成单个只读测试命令候选
def _python_candidate(
    boundary: WorkspaceBoundary,
    path: Path,
    relative: str,
) -> TestCommandCandidate | None:
    name = path.name.casefold()
    supported = name in {"pytest.ini", "tox.ini"}
    if name == "pyproject.toml":
        text = read_search_text(path)
        try:
            payload = tomllib.loads(text) if text is not None else {}
        except tomllib.TOMLDecodeError:
            return None
        supported = (
            isinstance(payload.get("tool"), dict)
            and isinstance(payload["tool"].get("pytest"), dict)
        ) or _contains_pytest(payload)
    if name == "setup.cfg":
        text = read_search_text(path) or ""
        supported = "[tool:pytest]" in text.casefold()
    if not supported:
        return None
    directory = path.parent
    argv: tuple[str, ...]
    if name == "tox.ini":
        argv = ("tox",)
        reason = "tox.ini declares tox environments"
    elif (directory / "uv.lock").is_file():
        argv = ("uv", "run", "pytest")
        reason = "pytest metadata with uv.lock"
    elif (directory / "poetry.lock").is_file():
        argv = ("poetry", "run", "pytest")
        reason = "pytest metadata with poetry.lock"
    else:
        argv = ("python", "-m", "pytest")
        reason = "pytest metadata"
    return TestCommandCandidate(
        ecosystem="python",
        argv=argv,
        cwd=_relative_directory(boundary, directory),
        source=relative,
        reason=reason,
    )


# 从 package.json 的 test script 生成包管理器候选但不解释或执行脚本正文
def _node_candidate(
    boundary: WorkspaceBoundary,
    path: Path,
    relative: str,
) -> TestCommandCandidate | None:
    text = read_search_text(path)
    try:
        payload: Any = json.loads(text) if text is not None else None
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    scripts = payload.get("scripts")
    test_script = scripts.get("test") if isinstance(scripts, dict) else None
    if not isinstance(test_script, str) or not test_script.strip():
        return None
    lowered = test_script.casefold()
    if "no test specified" in lowered:
        return None
    manager = _node_manager(path.parent, payload)
    return TestCommandCandidate(
        ecosystem="node",
        argv=(manager, "test"),
        cwd=_relative_directory(boundary, path.parent),
        source=relative,
        reason=f"package.json declares scripts.test via {manager}",
    )


# 从受支持 manifest 生成固定 argv 候选，绝不调用 shell 或运行测试
def _candidate_for_manifest(
    boundary: WorkspaceBoundary,
    path: Path,
    relative: str,
) -> TestCommandCandidate | None:
    name = path.name.casefold()
    if name in {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"}:
        return _python_candidate(boundary, path, relative)
    if name == "package.json":
        return _node_candidate(boundary, path, relative)
    directory = path.parent
    cwd = _relative_directory(boundary, directory)
    if path.suffix.casefold() in _DOTNET_SUFFIXES:
        return TestCommandCandidate(
            ecosystem="dotnet",
            argv=("dotnet", "test", path.name),
            cwd=cwd,
            source=relative,
            reason=".NET solution or project manifest",
        )
    fixed: dict[str, tuple[str, tuple[str, ...], str]] = {
        "cargo.toml": ("rust", ("cargo", "test"), "Cargo manifest"),
        "go.mod": ("go", ("go", "test", "./..."), "Go module"),
        "gemfile": (
            "ruby",
            ("bundle", "exec", "rake", "test"),
            "Ruby bundle manifest",
        ),
        "pom.xml": (
            "java",
            (("mvnw.cmd" if os.name == "nt" else "./mvnw"), "test")
            if (directory / ("mvnw.cmd" if os.name == "nt" else "mvnw")).is_file()
            else ("mvn", "test"),
            "Maven project",
        ),
        "build.gradle": (
            "java",
            (("gradlew.bat" if os.name == "nt" else "./gradlew"), "test")
            if (directory / ("gradlew.bat" if os.name == "nt" else "gradlew")).is_file()
            else ("gradle", "test"),
            "Gradle project",
        ),
        "build.gradle.kts": (
            "java",
            (("gradlew.bat" if os.name == "nt" else "./gradlew"), "test")
            if (directory / ("gradlew.bat" if os.name == "nt" else "gradlew")).is_file()
            else ("gradle", "test"),
            "Gradle project",
        ),
    }
    selected = fixed.get(name)
    if selected is None:
        return None
    ecosystem, argv, reason = selected
    return TestCommandCandidate(
        ecosystem=ecosystem,
        argv=argv,
        cwd=cwd,
        source=relative,
        reason=reason,
    )


# 有界扫描工作区 manifest 并仅返回命令候选和来源，不执行任何候选
def discover_test_commands(
    boundary: WorkspaceBoundary,
    *,
    max_depth: int = 5,
    max_manifests: int = 500,
    max_candidates: int = 100,
) -> TestCommandDiscovery:
    if max_depth < 0 or max_manifests < 1 or max_candidates < 1:
        raise ValueError("test command discovery limits are invalid")
    candidates: list[TestCommandCandidate] = []
    scanned = 0
    truncated = False
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for path, relative in iter_workspace_files(boundary, boundary.root):
        name = path.name.casefold()
        if name not in _MANIFEST_NAMES and path.suffix.casefold() not in _DOTNET_SUFFIXES:
            continue
        if len(Path(relative).parts) - 1 > max_depth:
            continue
        if scanned >= max_manifests:
            truncated = True
            break
        scanned += 1
        candidate = _candidate_for_manifest(boundary, path, relative)
        if candidate is None:
            continue
        source_digest = _manifest_digest(path)
        if source_digest is None:
            continue
        candidate = replace(candidate, source_digest=source_digest)
        try:
            render_test_command(candidate)
        except ValueError:
            continue
        key = (candidate.cwd, candidate.argv, candidate.source)
        if key in seen:
            continue
        seen.add(key)
        if len(candidates) >= max_candidates:
            truncated = True
            break
        candidates.append(candidate)
    candidates.sort(
        key=lambda item: (
            len(Path(item.cwd).parts),
            item.cwd.casefold(),
            item.ecosystem,
            item.source.casefold(),
        )
    )
    return TestCommandDiscovery(
        candidates=tuple(candidates),
        scanned_manifests=scanned,
        truncated=truncated,
    )

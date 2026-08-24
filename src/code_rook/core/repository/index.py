from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import subprocess
import threading
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_rook.core.processes import sanitized_shell_environment
from code_rook.core.repository.test_commands import (
    TestCommandDiscovery,
    discover_test_commands,
)
from code_rook.core.tools.builtin._search import (
    MAX_SEARCH_FILE_BYTES,
    iter_workspace_files,
    read_search_text,
)
from code_rook.core.workspace import WorkspaceBoundary

_LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}
_MANIFEST_NAMES = {
    "cargo.toml",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
}
_TEXT_EXTENSIONS = frozenset(_LANGUAGES) | {
    ".cfg",
    ".css",
    ".env.example",
    ".html",
    ".ini",
    ".json",
    ".md",
    ".ps1",
    ".sh",
    ".sql",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_TEXT_NAMES = {
    "agents.md",
    "changelog.md",
    "dockerfile",
    "license",
    "makefile",
    "readme.md",
}
_SENSITIVE_NAMES = {
    ".env",
    "api-token",
    "credentials.json",
    "ipc-token",
    "runtime.db",
}
_QUERY_TERM = re.compile(r"(?:_|[^\W\d])\w*", re.UNICODE)
_GENERIC_IDENTIFIER = r"(?:[$_]|[^\W\d])[\w$]*"
_GENERIC_SYMBOLS = (
    re.compile(rf"\b(?:export\s+)?(?:async\s+)?function\s+({_GENERIC_IDENTIFIER})\s*\(([^)]*)\)"),
    re.compile(rf"\b(?:export\s+)?(?:class|interface|enum|trait|struct)\s+({_GENERIC_IDENTIFIER})"),
    re.compile(rf"\b(?:export\s+)?(?:const|let|var|type)\s+({_GENERIC_IDENTIFIER})"),
    re.compile(rf"\bfn\s+({_GENERIC_IDENTIFIER})\s*\(([^)]*)\)"),
    re.compile(rf"\bfunc\s+(?:\([^)]*\)\s*)?({_GENERIC_IDENTIFIER})\s*\(([^)]*)\)"),
)
_IMPORT_PATTERN = re.compile(r"(?:from\s+['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"]\s*\))")
_CACHE_VERSION = 2
_MAX_CACHE_BYTES = 32 * 1024 * 1024
_MAX_SYMBOLS_PER_FILE = 128
_MAX_IMPORTS_PER_FILE = 128
_MONOREPO_CONTAINERS = frozenset(
    {"apps", "crates", "libs", "modules", "packages", "plugins", "services"}
)


@dataclass(frozen=True)
class RepositorySymbol:
    name: str
    kind: str
    path: str
    line: int
    end_line: int
    signature: str
    parent: str = ""
    reference_count: int = 0


@dataclass(frozen=True)
class RepositoryFile:
    path: str
    language: str
    size: int
    content_hash: str
    symbols: tuple[RepositorySymbol, ...]
    imports: tuple[str, ...] = ()


@dataclass(frozen=True)
class RepositorySnapshot:
    head: str
    worktree_hash: str
    files: tuple[RepositoryFile, ...]
    changed_paths: tuple[str, ...]
    cache_hits: int
    parsed_files: int
    truncated: bool = False
    partition_count: int = 0
    indexed_partitions: tuple[str, ...] = ()
    persistent_cache_hits: int = 0


@dataclass(frozen=True)
class ContextSelection:
    content: str
    paths: tuple[str, ...]
    reasons: tuple[dict[str, object], ...]
    budget_chars: int
    used_chars: int
    repository_hash: str
    cache_hits: int
    parsed_files: int


@dataclass(frozen=True)
class _CachedFile:
    signature: tuple[int, int, int]
    file: RepositoryFile


# 为 Git 仓库选择不进入工作树的默认持久缓存，非 Git 目录回退到用户缓存
def _default_cache_path(root: Path) -> Path:
    git_directory = root / ".git"
    if git_directory.is_dir() and not git_directory.is_symlink():
        return git_directory / "coderook" / "repository-index-v2.json"
    fingerprint = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:20]
    return Path("~/.coderook/cache/repositories").expanduser() / f"{fingerprint}.json"


# 把文件索引转换为不含源码正文的持久缓存对象
def _repository_file_payload(repository_file: RepositoryFile) -> dict[str, object]:
    return {
        "path": repository_file.path,
        "language": repository_file.language,
        "size": repository_file.size,
        "content_hash": repository_file.content_hash,
        "symbols": [symbol.__dict__ for symbol in repository_file.symbols],
        "imports": list(repository_file.imports),
    }


# 从持久缓存对象严格恢复文件摘要，字段损坏时拒绝该条目
def _repository_file_from_payload(payload: object) -> RepositoryFile | None:
    if not isinstance(payload, dict):
        return None
    try:
        symbols_raw = payload.get("symbols", [])
        if not isinstance(symbols_raw, list):
            return None
        symbols = tuple(
            RepositorySymbol(
                name=str(item["name"]),
                kind=str(item["kind"]),
                path=str(item["path"]),
                line=int(item["line"]),
                end_line=int(item["end_line"]),
                signature=str(item["signature"]),
                parent=str(item.get("parent", "")),
                reference_count=int(item.get("reference_count", 0)),
            )
            for item in symbols_raw
            if isinstance(item, dict)
        )
        imports_raw = payload.get("imports", [])
        if not isinstance(imports_raw, list):
            return None
        return RepositoryFile(
            path=str(payload["path"]),
            language=str(payload.get("language", "")),
            size=int(payload["size"]),
            content_hash=str(payload["content_hash"]),
            symbols=symbols,
            imports=tuple(str(value) for value in imports_raw),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


# 按 monorepo 常见容器的第二级目录划分索引分区
def _partition_key(relative: str) -> str:
    parts = Path(relative).parts
    if not parts:
        return "."
    first = parts[0]
    if len(parts) == 1:
        return "."
    if first.casefold() in _MONOREPO_CONTAINERS and len(parts) >= 3:
        return f"{first}/{parts[1]}"
    return first


# 在分区与全局双重上限内轮询选取文件，避免字典序靠前包独占索引
def _select_partitioned_candidates(
    candidates: Iterable[tuple[Path, str]],
    *,
    max_files: int,
    max_files_per_partition: int,
) -> tuple[list[tuple[Path, str]], bool, tuple[str, ...]]:
    partitions: dict[str, deque[tuple[Path, str]]] = {}
    stored = 0
    truncated = False
    for candidate in candidates:
        key = _partition_key(candidate[1])
        bucket = partitions.get(key)
        bucket_size = len(bucket) if bucket is not None else 0
        if bucket_size >= max_files_per_partition:
            truncated = True
            continue
        if stored >= max_files:
            donors = (
                (name, values)
                for name, values in partitions.items()
                if name != key and len(values) > bucket_size + 1
            )
            donor = max(
                donors,
                key=lambda item: (len(item[1]), item[0].casefold(), item[0]),
                default=None,
            )
            if donor is None:
                truncated = True
                continue
            donor[1].pop()
            stored -= 1
            truncated = True
        if bucket is None:
            bucket = partitions.setdefault(key, deque())
        bucket.append(candidate)
        stored += 1
    for bucket in partitions.values():
        ordered = sorted(bucket, key=lambda item: (item[1].casefold(), item[1]))
        bucket.clear()
        bucket.extend(ordered)
    selected: list[tuple[Path, str]] = []
    active = deque(sorted(partitions, key=lambda value: (value.casefold(), value)))
    while active and len(selected) < max_files:
        key = active.popleft()
        bucket = partitions[key]
        selected.append(bucket.popleft())
        if bucket:
            active.append(key)
    if active or any(partitions[key] for key in partitions):
        truncated = True
    selected.sort(key=lambda item: (item[1].casefold(), item[1]))
    indexed = tuple(
        sorted(
            {_partition_key(relative) for _path, relative in selected},
            key=lambda value: (value.casefold(), value),
        )
    )
    return selected, truncated, indexed


# 按 NUL 分隔符迭代 Git 原始输出而不复制全部路径记录
def _iter_nul_records(raw: bytes) -> Iterator[bytes]:
    start = 0
    while start < len(raw):
        end = raw.find(b"\0", start)
        if end < 0:
            end = len(raw)
        if end > start:
            yield raw[start:end]
        start = end + 1


# 按 NUL 边界严格解码 Git 路径，拒绝损坏字节而不制造替换字符碰撞
def _decode_git_path(value: bytes) -> str | None:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return None


# 返回文件扩展名对应的稳定语言名称
def _language(path: Path) -> str:
    return _LANGUAGES.get(path.suffix.casefold(), "")


# 对文本生成短内容摘要，作为文件级失效和收据标识
def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# 从 Python 参数和返回注解生成不包含函数体的签名
def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    rendered = f"{prefix} {node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        rendered += f" -> {ast.unparse(node.returns)}"
    return rendered[:500]


# 解析 Python 顶层与类成员符号、导入依赖和近似引用计数
def _parse_python(path: str, text: str) -> tuple[tuple[RepositorySymbol, ...], tuple[str, ...]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return (), ()
    references: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        else:
            name = ""
        if name:
            references[name] = references.get(name, 0) + 1

    imports: set[str] = set()
    symbols: list[RepositorySymbol] = []

    # 递归收集类、函数和异步函数，并保留父级限定名
    def _visit(nodes: list[ast.stmt], parent: str = "") -> None:
        for node in nodes:
            if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
                return
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module)
                elif isinstance(node, ast.Import):
                    imports.update(alias.name for alias in node.names)
                continue
            if isinstance(node, ast.ClassDef):
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind="class",
                        path=path,
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        signature=f"class {node.name}",
                        parent=parent,
                        reference_count=max(0, references.get(node.name, 0) - 1),
                    )
                )
                _visit(node.body, ".".join(filter(None, (parent, node.name))))
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind="method" if parent else "function",
                        path=path,
                        line=node.lineno,
                        end_line=node.end_lineno or node.lineno,
                        signature=_python_signature(node),
                        parent=parent,
                        reference_count=max(0, references.get(node.name, 0) - 1),
                    )
                )

    _visit(tree.body)
    return (
        tuple(symbols),
        tuple(sorted(imports, key=str.casefold)[:_MAX_IMPORTS_PER_FILE]),
    )


# 用轻量语法模式为常见非 Python 语言提取声明与模块依赖
def _parse_generic(path: str, text: str) -> tuple[tuple[RepositorySymbol, ...], tuple[str, ...]]:
    symbols: list[RepositorySymbol] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if len(symbols) >= _MAX_SYMBOLS_PER_FILE:
            break
        for pattern in _GENERIC_SYMBOLS:
            match = pattern.search(line)
            if match is None:
                continue
            name = match.group(1)
            kind = (
                "class"
                if re.search(r"\b(class|interface|enum|trait|struct)\b", line)
                else "function"
            )
            if re.search(r"\b(const|let|var|type)\b", line):
                kind = "variable"
            signature = match.group(0).strip()[:300]
            symbols.append(
                RepositorySymbol(
                    name=name,
                    kind=kind,
                    path=path,
                    line=line_number,
                    end_line=line_number,
                    signature=signature,
                    reference_count=max(0, text.count(name) - 1),
                )
            )
            break
    imports = {
        next(group for group in match.groups() if group) for match in _IMPORT_PATTERN.finditer(text)
    }
    return (
        tuple(symbols),
        tuple(sorted(imports, key=str.casefold)[:_MAX_IMPORTS_PER_FILE]),
    )


# 为单个工作区文件生成语言、hash、符号和依赖摘要
def _parse_file(path: Path, relative: str) -> RepositoryFile | None:
    text = read_search_text(path)
    if text is None:
        return None
    language = _language(path)
    if language == "Python":
        symbols, imports = _parse_python(relative, text)
    elif language:
        symbols, imports = _parse_generic(relative, text)
    else:
        symbols, imports = (), ()
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return RepositoryFile(
        path=relative,
        language=language,
        size=size,
        content_hash=_content_hash(text),
        symbols=symbols,
        imports=imports,
    )


# 安全执行只读 Git 查询，非仓库或命令失败时返回空字节
def _git_bytes(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-c",
                "core.fsmonitor=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                *args,
            ],
            cwd=root,
            env=sanitized_shell_environment(),
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return b""
    return result.stdout if result.returncode == 0 else b""


# 判断路径是否属于可发送给模型的非敏感文本源码
def _is_indexable_path(relative: str) -> bool:
    path = Path(relative)
    parts = tuple(part.casefold() for part in path.parts)
    name = path.name.casefold()
    if any(ord(character) < 32 for character in relative):
        return False
    if ".coderook" in parts or name in _SENSITIVE_NAMES or name.startswith(".env."):
        return False
    return name in _TEXT_NAMES or path.suffix.casefold() in _TEXT_EXTENSIONS


# 把 Git NUL 路径流转换为工作区内、尺寸受限的索引候选
def _git_repository_candidates(
    boundary: WorkspaceBoundary,
    raw: bytes,
) -> Iterator[tuple[Path, str]]:
    for value in _iter_nul_records(raw):
        relative = _decode_git_path(value)
        if relative is None or not _is_indexable_path(relative):
            continue
        try:
            path = boundary.resolve(relative)
            if path.is_file() and path.stat().st_size <= MAX_SEARCH_FILE_BYTES:
                yield path, relative
        except (OSError, PermissionError):
            continue


# 以统一忽略规则流式产生非 Git 工作区候选并排除超大文件
def _workspace_repository_candidates(
    boundary: WorkspaceBoundary,
) -> Iterator[tuple[Path, str]]:
    for path, relative in iter_workspace_files(
        boundary,
        boundary.root,
        include_hidden=True,
    ):
        if not _is_indexable_path(relative):
            continue
        try:
            if path.stat().st_size <= MAX_SEARCH_FILE_BYTES:
                yield path, relative
        except OSError:
            continue


# 优先返回 Git 已跟踪与未忽略文件，非 Git 工作区回退统一忽略规则
def _repository_files(
    boundary: WorkspaceBoundary,
    max_files: int,
    max_files_per_partition: int,
) -> tuple[list[tuple[Path, str]], bool, tuple[str, ...]]:
    raw = _git_bytes(
        boundary.root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    if raw:
        candidates: Iterable[tuple[Path, str]] = _git_repository_candidates(boundary, raw)
    else:
        candidates = _workspace_repository_candidates(boundary)
    return _select_partitioned_candidates(
        candidates,
        max_files=max_files,
        max_files_per_partition=max_files_per_partition,
    )


# 解析 porcelain v1 的 NUL 格式并正确跳过 rename/copy 的第二路径字段
def _parse_git_status_paths(raw: bytes) -> tuple[str, ...]:
    records = iter(_iter_nul_records(raw))
    changed: set[str] = set()
    for record in records:
        if len(record) < 4 or record[2:3] != b" ":
            continue
        status = record[:2].decode("ascii", errors="ignore")
        relative = _decode_git_path(record[3:])
        if relative:
            changed.add(relative)
        if "R" in status or "C" in status:
            next(records, None)
    return tuple(sorted(changed, key=lambda value: (value.casefold(), value)))


# 读取当前提交和脏工作区路径，供上下文排序与缓存收据使用
def _git_state(root: Path) -> tuple[str, tuple[str, ...]]:
    head = _git_bytes(root, "rev-parse", "HEAD").decode("ascii", errors="replace").strip()
    status = _git_bytes(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return head, _parse_git_status_paths(status)


class RepositoryIndex:
    # 初始化分区有界索引并从不含源码正文的持久缓存恢复文件摘要
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        max_files: int = 5_000,
        max_files_per_partition: int | None = None,
        cache_path: Path | None = None,
    ) -> None:
        if max_files < 1:
            raise ValueError("max_files must be positive")
        partition_limit = (
            min(1_000, max_files) if max_files_per_partition is None else max_files_per_partition
        )
        if partition_limit < 1:
            raise ValueError("max_files_per_partition must be positive")
        self._boundary = boundary
        self._max_files = max_files
        self._max_files_per_partition = partition_limit
        self._cache_path = cache_path or _default_cache_path(boundary.root)
        self._cache = self._load_persistent_cache()
        self._persistent_entries = set(self._cache)
        self._snapshot: RepositorySnapshot | None = None
        self._lock = threading.RLock()
        self._prewarm_task: asyncio.Task[RepositorySnapshot] | None = None

    # 返回当前索引使用的持久缓存路径，便于诊断和测试隔离
    @property
    def cache_path(self) -> Path:
        return self._cache_path

    # 从有界 JSON 文档恢复合法缓存条目，任何损坏均安全回退为空缓存
    def _load_persistent_cache(self) -> dict[str, _CachedFile]:
        try:
            if not self._cache_path.is_file() or self._cache_path.stat().st_size > _MAX_CACHE_BYTES:
                return {}
            payload: Any = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if (
            not isinstance(payload, dict)
            or payload.get("version") != _CACHE_VERSION
            or payload.get("root") != str(self._boundary.root)
            or not isinstance(payload.get("files"), list)
        ):
            return {}
        restored: dict[str, _CachedFile] = {}
        for item in payload["files"][: self._max_files]:
            if not isinstance(item, dict):
                continue
            signature = item.get("signature")
            repository_file = _repository_file_from_payload(item.get("file"))
            if repository_file is None or not isinstance(signature, list) or len(signature) != 3:
                continue
            try:
                restored[repository_file.path] = _CachedFile(
                    (
                        int(signature[0]),
                        int(signature[1]),
                        int(signature[2]),
                    ),
                    repository_file,
                )
            except (TypeError, ValueError, OverflowError):
                continue
        return restored

    # 原子保存文件摘要和 stat 签名，不持久化任何源码正文
    def _save_persistent_cache(self, cache: dict[str, _CachedFile]) -> None:
        entries: list[dict[str, object]] = []
        used_bytes = 256
        for _path, cached in sorted(
            cache.items(),
            key=lambda item: (item[0].casefold(), item[0]),
        ):
            entry: dict[str, object] = {
                "signature": list(cached.signature),
                "file": _repository_file_payload(cached.file),
            }
            entry_bytes = len(
                json.dumps(
                    entry,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if used_bytes + entry_bytes + 1 > _MAX_CACHE_BYTES:
                break
            entries.append(entry)
            used_bytes += entry_bytes + 1
        payload = {
            "version": _CACHE_VERSION,
            "root": str(self._boundary.root),
            "files": entries,
            "truncated": len(entries) < len(cache),
        }
        temporary = self._cache_path.with_name(
            f".{self._cache_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            temporary.replace(self._cache_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    # 清理由当前实例创建且已完成的预热任务引用
    def _clear_prewarm_task(self, task: asyncio.Task[RepositorySnapshot]) -> None:
        if self._prewarm_task is task:
            self._prewarm_task = None

    # 在当前事件循环启动去重的后台预热，不阻塞调用方
    def start_prewarm(self) -> asyncio.Task[RepositorySnapshot]:
        current = self._prewarm_task
        if current is not None and not current.done():
            return current
        task = asyncio.create_task(
            asyncio.to_thread(self.refresh),
            name="coderook-repository-prewarm",
        )
        self._prewarm_task = task
        task.add_done_callback(self._clear_prewarm_task)
        return task

    # 等待共享后台预热完成并返回所得快照
    async def prewarm(self) -> RepositorySnapshot:
        return await asyncio.shield(self.start_prewarm())

    # 按 mtime/size 复用解析结果，并以每文件内容 hash 生成工作区版本
    def refresh(self) -> RepositorySnapshot:
        with self._lock:
            candidates, truncated, indexed_partitions = _repository_files(
                self._boundary,
                self._max_files,
                self._max_files_per_partition,
            )
            head, changed_paths = _git_state(self._boundary.root)
            files: list[RepositoryFile] = []
            next_cache: dict[str, _CachedFile] = {}
            cache_hits = 0
            persistent_cache_hits = 0
            parsed_files = 0
            for path, relative in candidates:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                cached = self._cache.get(relative)
                repository_file: RepositoryFile | None
                if cached is not None and cached.signature == signature:
                    repository_file = cached.file
                    cache_hits += 1
                    if relative in self._persistent_entries:
                        persistent_cache_hits += 1
                else:
                    repository_file = _parse_file(path, relative)
                    parsed_files += 1
                if repository_file is None:
                    continue
                files.append(repository_file)
                next_cache[relative] = _CachedFile(signature, repository_file)
            digest = hashlib.sha256()
            digest.update(head.encode("utf-8"))
            for repository_file in files:
                digest.update(repository_file.path.encode("utf-8"))
                digest.update(repository_file.content_hash.encode("ascii"))
            snapshot = RepositorySnapshot(
                head=head,
                worktree_hash=digest.hexdigest()[:16],
                files=tuple(files),
                changed_paths=changed_paths,
                cache_hits=cache_hits,
                parsed_files=parsed_files,
                truncated=truncated,
                partition_count=len(indexed_partitions),
                indexed_partitions=indexed_partitions,
                persistent_cache_hits=persistent_cache_hits,
            )
            self._cache = next_cache
            self._persistent_entries.clear()
            self._snapshot = snapshot
            self._save_persistent_cache(next_cache)
            return snapshot

    # 返回最近快照，尚未索引时立即构建
    def snapshot(self) -> RepositorySnapshot:
        return self._snapshot or self.refresh()

    # 有界发现多语言项目测试命令，仅返回候选和 manifest 来源
    def test_commands(
        self,
        *,
        max_candidates: int = 100,
    ) -> TestCommandDiscovery:
        return discover_test_commands(
            self._boundary,
            max_candidates=max_candidates,
        )

    # 按名称、限定名和路径执行确定性符号搜索
    def search_symbols(self, query: str, *, limit: int = 50) -> tuple[RepositorySymbol, ...]:
        snapshot = self.refresh()
        needle = query.casefold().strip()
        ranked: list[tuple[int, RepositorySymbol]] = []
        for repository_file in snapshot.files:
            for symbol in repository_file.symbols:
                qualified = ".".join(filter(None, (symbol.parent, symbol.name)))
                haystacks = (symbol.name.casefold(), qualified.casefold(), symbol.path.casefold())
                if needle and not any(needle in value for value in haystacks):
                    continue
                if symbol.name.casefold() == needle:
                    score = 100
                elif symbol.name.casefold().startswith(needle):
                    score = 70
                else:
                    score = 40
                if needle in symbol.path.casefold():
                    score += 10
                ranked.append((score, symbol))
        ranked.sort(
            key=lambda item: (
                -item[0],
                item[1].path.casefold(),
                item[1].line,
                item[1].name,
            )
        )
        return tuple(symbol for _score, symbol in ranked[:limit])

    # 对标识符执行逐行引用搜索，无法语法解析的语言也能稳定回退
    def find_references(
        self,
        symbol: str,
        *,
        path: str = ".",
        limit: int = 100,
    ) -> tuple[dict[str, object], ...]:
        if not symbol.isidentifier():
            raise ValueError("symbol must be a plain identifier")
        search_root = self._boundary.resolve(path)
        expression = re.compile(rf"\b{re.escape(symbol)}\b")
        matches: list[dict[str, object]] = []
        for repository_file in self.refresh().files:
            candidate = self._boundary.resolve(repository_file.path)
            try:
                candidate.relative_to(search_root)
            except ValueError:
                continue
            text = read_search_text(candidate)
            if text is None:
                continue
            declarations = {(item.line, item.name) for item in repository_file.symbols}
            for line_number, line in enumerate(text.splitlines(), start=1):
                for match in expression.finditer(line):
                    matches.append(
                        {
                            "path": repository_file.path,
                            "line": line_number,
                            "column": match.start() + 1,
                            "kind": (
                                "declaration"
                                if (line_number, symbol) in declarations
                                else "reference"
                            ),
                            "content": line.strip()[:500],
                        }
                    )
                    if len(matches) >= limit:
                        return tuple(matches)
        return tuple(matches)

    # 在字符预算内选择与任务最相关的文件摘要并记录每项选择原因
    def select_context(self, query: str, *, budget_chars: int = 12_000) -> ContextSelection:
        snapshot = self.refresh()
        terms = {term.casefold() for term in _QUERY_TERM.findall(query)}
        changed = set(snapshot.changed_paths)
        ranked: list[tuple[int, RepositoryFile, list[str]]] = []
        for repository_file in snapshot.files:
            score = 0
            reasons: list[str] = []
            path_folded = repository_file.path.casefold()
            symbol_names = {symbol.name.casefold() for symbol in repository_file.symbols}
            path_hits = sorted(term for term in terms if term in path_folded)
            symbol_hits = sorted(
                term for term in terms if any(term in name for name in symbol_names)
            )
            if path_hits:
                score += 30 + 5 * len(path_hits)
                reasons.append("query_path:" + ",".join(path_hits[:4]))
            if symbol_hits:
                score += 50 + 8 * len(symbol_hits)
                reasons.append("query_symbol:" + ",".join(symbol_hits[:4]))
            if repository_file.path in changed:
                score += 45
                reasons.append("git_changed")
            if Path(repository_file.path).name.casefold() in _MANIFEST_NAMES:
                score += 18
                reasons.append("manifest")
            if path_folded in {"readme.md", "agents.md"}:
                score += 20
                reasons.append("repository_entrypoint")
            if not reasons:
                continue
            ranked.append((score, repository_file, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1].path.casefold(), item[1].path))

        header = (
            "## Repository Map\n"
            f"- version: `{snapshot.worktree_hash}`; head: `{snapshot.head or 'unversioned'}`\n"
            f"- indexed files: {len(snapshot.files)}; changed: {len(snapshot.changed_paths)}; "
            f"partitions: {snapshot.partition_count}; cache hits: {snapshot.cache_hits} "
            f"(persistent: {snapshot.persistent_cache_hits}); parsed: {snapshot.parsed_files}\n"
            "- summaries below are selected for the current request; "
            "use Repository/File tools for source text.\n"
        )
        content = header
        selected_paths: list[str] = []
        selection_reasons: list[dict[str, object]] = []
        for score, repository_file, reasons in ranked:
            symbol_text = (
                "; ".join(symbol.signature for symbol in repository_file.symbols[:12])
                or "no indexed symbols"
            )
            imports = ", ".join(repository_file.imports[:8])
            block = (
                f"\n### {repository_file.path}\n"
                f"language={repository_file.language or 'text'} score={score} "
                f"reason={','.join(reasons)}\n"
                f"symbols: {symbol_text}\n"
            )
            if imports:
                block += f"imports: {imports}\n"
            if len(content) + len(block) > budget_chars:
                continue
            content += block
            selected_paths.append(repository_file.path)
            selection_reasons.append(
                {
                    "path": repository_file.path,
                    "score": score,
                    "reasons": reasons,
                }
            )
        return ContextSelection(
            content=content.rstrip(),
            paths=tuple(selected_paths),
            reasons=tuple(selection_reasons),
            budget_chars=budget_chars,
            used_chars=len(content.rstrip()),
            repository_hash=snapshot.worktree_hash,
            cache_hits=snapshot.cache_hits,
            parsed_files=snapshot.parsed_files,
        )

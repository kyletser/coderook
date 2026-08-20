from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

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
_QUERY_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GENERIC_SYMBOLS = (
    re.compile(r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)"),
    re.compile(r"\b(?:export\s+)?(?:class|interface|enum|trait|struct)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:export\s+)?(?:const|let|var|type)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\bfn\s+([A-Za-z_][\w]*)\s*\(([^)]*)\)"),
    re.compile(r"\bfunc\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\(([^)]*)\)"),
)
_IMPORT_PATTERN = re.compile(
    r"(?:from\s+['\"]([^'\"]+)['\"]|require\(\s*['\"]([^'\"]+)['\"]\s*\))"
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
    signature: tuple[int, int]
    file: RepositoryFile


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
    return rendered


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
    return tuple(symbols), tuple(sorted(imports, key=str.casefold))


# 用轻量语法模式为常见非 Python 语言提取声明与模块依赖
def _parse_generic(path: str, text: str) -> tuple[tuple[RepositorySymbol, ...], tuple[str, ...]]:
    symbols: list[RepositorySymbol] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
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
                    reference_count=max(0, len(re.findall(rf"\b{re.escape(name)}\b", text)) - 1),
                )
            )
            break
    imports = {
        next(group for group in match.groups() if group)
        for match in _IMPORT_PATTERN.finditer(text)
    }
    return tuple(symbols), tuple(sorted(imports, key=str.casefold))


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
            ["git", *args],
            cwd=root,
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


# 优先返回 Git 已跟踪与未忽略文件，非 Git 工作区回退统一忽略规则
def _repository_files(
    boundary: WorkspaceBoundary,
    max_files: int,
) -> tuple[list[tuple[Path, str]], bool]:
    raw = _git_bytes(
        boundary.root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    candidates: list[tuple[Path, str]] = []
    if raw:
        for value in raw.split(b"\0"):
            if not value:
                continue
            relative = value.decode("utf-8", errors="replace").replace("\\", "/")
            if not _is_indexable_path(relative):
                continue
            try:
                path = boundary.resolve(relative)
                if path.is_file() and path.stat().st_size <= MAX_SEARCH_FILE_BYTES:
                    candidates.append((path, relative))
            except (OSError, PermissionError):
                continue
    else:
        candidates.extend(
            (path, relative)
            for path, relative in iter_workspace_files(
                boundary,
                boundary.root,
                include_hidden=True,
            )
            if _is_indexable_path(relative)
        )
    candidates.sort(key=lambda item: (item[1].casefold(), item[1]))
    return candidates[:max_files], len(candidates) > max_files


# 读取当前提交和脏工作区路径，供上下文排序与缓存收据使用
def _git_state(root: Path) -> tuple[str, tuple[str, ...]]:
    head = _git_bytes(root, "rev-parse", "HEAD").decode("ascii", errors="replace").strip()
    status = _git_bytes(root, "status", "--porcelain=v1", "--untracked-files=all")
    changed: set[str] = set()
    for raw_line in status.decode("utf-8", errors="replace").splitlines():
        value = raw_line[3:] if len(raw_line) > 3 else ""
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        value = value.strip('"').replace("\\", "/")
        if value:
            changed.add(value)
    return head, tuple(sorted(changed, key=str.casefold))


class RepositoryIndex:
    # 初始化 daemon 内共享的增量索引，不向工作区写隐藏缓存
    def __init__(self, boundary: WorkspaceBoundary, *, max_files: int = 5_000) -> None:
        self._boundary = boundary
        self._max_files = max_files
        self._cache: dict[str, _CachedFile] = {}
        self._snapshot: RepositorySnapshot | None = None
        self._lock = threading.RLock()

    # 按 mtime/size 复用解析结果，并以每文件内容 hash 生成工作区版本
    def refresh(self) -> RepositorySnapshot:
        with self._lock:
            candidates, truncated = _repository_files(self._boundary, self._max_files)
            head, changed_paths = _git_state(self._boundary.root)
            files: list[RepositoryFile] = []
            next_cache: dict[str, _CachedFile] = {}
            cache_hits = 0
            parsed_files = 0
            for path, relative in candidates:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                signature = (stat.st_mtime_ns, stat.st_size)
                cached = self._cache.get(relative)
                repository_file: RepositoryFile | None
                if cached is not None and cached.signature == signature:
                    repository_file = cached.file
                    cache_hits += 1
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
            )
            self._cache = next_cache
            self._snapshot = snapshot
            return snapshot

    # 返回最近快照，尚未索引时立即构建
    def snapshot(self) -> RepositorySnapshot:
        return self._snapshot or self.refresh()

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
        if not _IDENTIFIER.fullmatch(symbol):
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
            f"cache hits: {snapshot.cache_hits}; parsed: {snapshot.parsed_files}\n"
            "- summaries below are selected for the current request; "
            "use Repository/File tools for source text.\n"
        )
        content = header
        selected_paths: list[str] = []
        selection_reasons: list[dict[str, object]] = []
        for score, repository_file, reasons in ranked:
            symbol_text = (
                "; ".join(
                    symbol.signature for symbol in repository_file.symbols[:12]
                )
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

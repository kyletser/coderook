from __future__ import annotations

import json
import subprocess
from pathlib import Path

from code_rook.core.repository import RepositoryIndex, RepositoryTool
from code_rook.core.workspace import WorkspaceBoundary


# 初始化包含 Python、TypeScript、清单和忽略文件的最小 Git 仓库
def _make_repository(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "service.py").write_text(
        "import os\n\n"
        "class Worker:\n"
        "    async def run(self, value: str) -> str:\n"
        "        return helper(value)\n\n"
        "def helper(value: str) -> str:\n"
        "    return os.path.basename(value)\n",
        encoding="utf-8",
    )
    (root / "src" / "client.ts").write_text(
        "export class ApiClient {}\n"
        "export async function fetchData(url: string) { return url; }\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("def hidden(): pass\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=do-not-index\n", encoding="utf-8")
    (root / ".coderook").mkdir()
    (root / ".coderook" / "context.md").write_text(
        "private workspace state\n",
        encoding="utf-8",
    )
    (root / "image.png").write_bytes(b"not-a-real-image")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


# 功能：验证仓库索引尊重 Git ignore 并提取多语言符号、签名、依赖与引用计数
# 设计：使用真实 git ls-files 路径而非 mock，覆盖未提交仓库和常见 Python/TypeScript 声明
def test_repository_index_builds_git_aware_symbol_map(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    index = RepositoryIndex(WorkspaceBoundary(tmp_path))

    snapshot = index.refresh()
    paths = {item.path for item in snapshot.files}
    worker = next(symbol for symbol in index.search_symbols("Worker") if symbol.name == "Worker")
    fetch = next(symbol for symbol in index.search_symbols("fetchData") if symbol.name == "fetchData")

    assert "ignored.py" not in paths
    assert ".env" not in paths
    assert ".coderook/context.md" not in paths
    assert "image.png" not in paths
    assert {"README.md", "pyproject.toml", "src/service.py", "src/client.ts"} <= paths
    assert worker.kind == "class"
    assert worker.path == "src/service.py"
    assert fetch.signature.startswith("export async function")
    service = next(item for item in snapshot.files if item.path == "src/service.py")
    assert "os" in service.imports
    assert any(symbol.signature.startswith("async def run") for symbol in service.symbols)


# 功能：验证增量刷新复用未变化文件并在内容变化后只重建对应文件
# 设计：连续刷新同一实例后修改一个文件，直接断言 cache_hits、parsed_files 和工作区 hash
def test_repository_index_incremental_cache_invalidates_changed_file(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    index = RepositoryIndex(WorkspaceBoundary(tmp_path))

    first = index.refresh()
    second = index.refresh()
    service = tmp_path / "src" / "service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + "\ndef newly_added() -> None:\n    pass\n",
        encoding="utf-8",
    )
    third = index.refresh()

    assert first.parsed_files == len(first.files)
    assert second.parsed_files == 0
    assert second.cache_hits == len(second.files)
    assert third.parsed_files == 1
    assert third.cache_hits == len(third.files) - 1
    assert third.worktree_hash != second.worktree_hash
    assert index.search_symbols("newly_added")[0].path == "src/service.py"


# 功能：验证任务相关上下文遵守预算并为每个入选路径记录可解释原因
# 设计：查询同时命中路径和符号，检查首选源码、预算上界及 manifest/entrypoint 基线
def test_repository_context_selection_is_budgeted_and_explainable(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    index = RepositoryIndex(WorkspaceBoundary(tmp_path))

    selection = index.select_context("fix Worker in service", budget_chars=3_000)

    assert selection.used_chars <= selection.budget_chars
    assert selection.paths[0] == "src/service.py"
    service_reason = next(
        item for item in selection.reasons if item["path"] == "src/service.py"
    )
    assert "query_path:service" in service_reason["reasons"]
    assert "query_symbol:worker" in service_reason["reasons"]
    assert "pyproject.toml" in selection.paths
    assert selection.repository_hash


# 功能：验证 Repository 工具输出结构化符号与文本回退引用结果
# 设计：通过真实异步工具入口执行两个 action，覆盖 schema 后的 JSON 协议而非直接调用索引
async def test_repository_tool_searches_symbols_and_references(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    tool = RepositoryTool(RepositoryIndex(WorkspaceBoundary(tmp_path)))

    symbols_result = await tool.invoke({"action": "symbols", "query": "helper"})
    references_result = await tool.invoke(
        {"action": "references", "symbol": "helper", "path": "src"}
    )
    symbols = json.loads(symbols_result.content)
    references = json.loads(references_result.content)

    assert symbols["backend"] == "syntax-index"
    assert symbols["symbols"][0]["name"] == "helper"
    assert references["backend"] == "syntax-index+text-fallback"
    assert {match["kind"] for match in references["matches"]} == {
        "declaration",
        "reference",
    }

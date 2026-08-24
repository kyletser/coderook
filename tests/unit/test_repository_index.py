from __future__ import annotations

import json
import subprocess
from pathlib import Path

from code_rook.core.repository import RepositoryIndex, RepositoryTool
from code_rook.core.repository import index as repository_index_module
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
        "export class ApiClient {}\nexport async function fetchData(url: string) { return url; }\n",
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
    fetch = next(
        symbol for symbol in index.search_symbols("fetchData") if symbol.name == "fetchData"
    )

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
    service_reason = next(item for item in selection.reasons if item["path"] == "src/service.py")
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


# 功能：验证 Unicode 文件路径、Python 标识符、查询词和引用搜索全链路保持原文
# 设计：在真实 Git 工作区创建中文目录与函数，分别走快照、符号、引用和上下文选择接口
def test_repository_index_supports_unicode_paths_and_queries(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    directory = tmp_path / "模块"
    directory.mkdir()
    target = directory / "服务 甲.py"
    target.write_text(
        "def 计算总和(数值: int) -> int:\n    return 数值 + 1\n\n结果 = 计算总和(2)\n",
        encoding="utf-8",
    )
    index = RepositoryIndex(WorkspaceBoundary(tmp_path))

    snapshot = index.refresh()
    symbols = index.search_symbols("计算总和")
    references = index.find_references("计算总和")
    selection = index.select_context("修复模块中的计算总和")

    assert "模块/服务 甲.py" in {item.path for item in snapshot.files}
    assert symbols[0].name == "计算总和"
    assert symbols[0].path == "模块/服务 甲.py"
    assert len(references) == 2
    assert selection.paths == ("模块/服务 甲.py",)
    assert snapshot.changed_paths == ("模块/服务 甲.py",)


# 功能：验证 Git porcelain 的 NUL 解析不按换行、引号或箭头文本猜测路径
# 设计：直接构造 rename 双记录和含空格/箭头的 UTF-8 路径，锁定目标路径与来源字段边界
def test_git_status_paths_use_nul_records_for_renames_and_unicode() -> None:
    payload = ("R  src/新 -> 名.py\0src/旧 名.py\0?? tests/空 格.py\0").encode()

    changed = repository_index_module._parse_git_status_paths(payload)

    assert changed == ("src/新 -> 名.py", "tests/空 格.py")


# 功能：验证大型 monorepo 按包分区且同时受每分区和全局文件上限约束
# 设计：建立三个 packages 子项目，每个文件数均超限，断言轮询结果覆盖全部分区而非偏向首包
def test_repository_index_partitions_and_bounds_monorepo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for package in ("alpha", "beta", "gamma"):
        directory = tmp_path / "packages" / package
        directory.mkdir(parents=True)
        for index in range(4):
            (directory / f"module_{index}.py").write_text(
                f"def {package}_{index}():\n    return {index}\n",
                encoding="utf-8",
            )
    index = RepositoryIndex(
        WorkspaceBoundary(tmp_path),
        max_files=6,
        max_files_per_partition=2,
    )

    snapshot = index.refresh()

    assert len(snapshot.files) == 6
    assert snapshot.truncated is True
    assert snapshot.partition_count == 3
    assert snapshot.indexed_partitions == (
        "packages/alpha",
        "packages/beta",
        "packages/gamma",
    )
    assert {"/".join(item.path.split("/")[:2]) for item in snapshot.files} == set(
        snapshot.indexed_partitions
    )


# 功能：验证索引摘要跨 RepositoryIndex 实例持久复用且缓存不保存源码正文
# 设计：两个实例共享显式 .git 缓存，第二次断言全命中，并扫描 JSON 排除函数体内容
def test_repository_index_persists_bounded_summary_cache(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    cache_path = tmp_path / ".git" / "coderook" / "test-index.json"
    first = RepositoryIndex(WorkspaceBoundary(tmp_path), cache_path=cache_path)
    initial = first.refresh()

    second = RepositoryIndex(WorkspaceBoundary(tmp_path), cache_path=cache_path)
    restored = second.refresh()

    assert restored.parsed_files == 0
    assert restored.persistent_cache_hits == len(initial.files)
    assert restored.cache_hits == len(initial.files)
    cache_text = cache_path.read_text(encoding="utf-8")
    assert "return helper(value)" not in cache_text
    assert "src/service.py" in cache_text


# 功能：验证后台预热接口对同一实例的并发请求去重并返回完整快照
# 设计：连续启动两次预热并比较 Task 身份，随后等待结果检查索引已可直接读取
async def test_repository_index_background_prewarm_is_deduplicated(tmp_path: Path) -> None:
    _make_repository(tmp_path)
    index = RepositoryIndex(WorkspaceBoundary(tmp_path))

    first = index.start_prewarm()
    second = index.start_prewarm()
    snapshot = await first

    assert first is second
    assert snapshot.files
    assert index.snapshot().worktree_hash == snapshot.worktree_hash

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

import code_rook.core.tools.builtin.glob as glob_module
import code_rook.core.tools.builtin.grep as grep_module
from code_rook.core.config import CodeRookConfig
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.builtin.glob import GlobTool
from code_rook.core.tools.builtin.grep import GrepTool


def _payload(content: str) -> dict:
    return json.loads(content)


def _make_search_tree(root: Path) -> None:
    (root / "src" / "nested").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "Alpha target\n你好 target target\n", encoding="utf-8"
    )
    (root / "src" / "nested" / "util.py").write_text("alpha helper\n", encoding="utf-8")
    (root / "src" / "notes.txt").write_text("target in text\n", encoding="utf-8")
    (root / "ignored.py").write_text("target ignored\n", encoding="utf-8")
    (root / ".hidden.py").write_text("target hidden\n", encoding="utf-8")
    (root / "binary.py").write_bytes(b"target\x00binary")
    (root / "node_modules" / "pkg" / "index.py").write_text("target\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")


# 功能：Python fallback 支持 **、稳定排序、gitignore、默认排除和隐藏文件策略
async def test_glob_python_backend_respects_search_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_search_tree(tmp_path)
    monkeypatch.setattr(glob_module, "ripgrep_path", lambda: None)

    result = await GlobTool(workspace_root=tmp_path).invoke({"pattern": "**/*.py"})
    data = _payload(result.content)

    assert data["backend"] == "python"
    assert data["files"] == ["binary.py", "src/main.py", "src/nested/util.py"]
    assert not data["truncated"]


# 功能：include_hidden 只放开隐藏文件，仍不绕过 .gitignore 和默认目录排除
async def test_glob_include_hidden_and_limit_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_search_tree(tmp_path)
    monkeypatch.setattr(glob_module, "ripgrep_path", lambda: None)

    result = await GlobTool(workspace_root=tmp_path).invoke({
        "pattern": "**/*.py",
        "include_hidden": True,
        "limit": 1,
    })
    data = _payload(result.content)

    assert data["files"] == [".hidden.py"]
    assert data["count"] == 1
    assert data["truncated"] is True


# 功能：Glob 的搜索根和 pattern 都不能逃逸工作区
async def test_glob_rejects_workspace_escape(tmp_path: Path) -> None:
    tool = GlobTool(workspace_root=tmp_path)
    with pytest.raises(PermissionError):
        await tool.invoke({"pattern": "**/*", "path": ".."})
    with pytest.raises(ValidationError):
        await tool.invoke({"pattern": "../*.py"})


# 功能：Python Grep 返回 path/line/column/content，并正确计算 Unicode 字符列
async def test_grep_python_backend_returns_structured_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_search_tree(tmp_path)
    monkeypatch.setattr(grep_module, "ripgrep_path", lambda: None)

    result = await GrepTool(workspace_root=tmp_path).invoke({
        "pattern": "target",
        "glob": "**/*.py",
    })
    data = _payload(result.content)

    assert data["backend"] == "python"
    assert [match["path"] for match in data["matches"]] == ["src/main.py", "src/main.py"]
    assert data["matches"][0] == {
        "path": "src/main.py",
        "line": 1,
        "column": 7,
        "content": "Alpha target",
        "content_truncated": False,
    }
    assert data["matches"][1]["column"] == 4
    assert data["match_count"] == 3


# 功能：Grep 支持忽略大小写、files_with_matches 与 count 三种稳定输出
async def test_grep_python_output_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_search_tree(tmp_path)
    monkeypatch.setattr(grep_module, "ripgrep_path", lambda: None)
    tool = GrepTool(workspace_root=tmp_path)

    files = _payload((await tool.invoke({
        "pattern": "alpha",
        "case_sensitive": False,
        "output_mode": "files_with_matches",
    })).content)
    counts = _payload((await tool.invoke({
        "pattern": "target",
        "glob": "**/*.py",
        "output_mode": "count",
    })).content)

    assert files["matches"] == [{"path": "src/main.py"}, {"path": "src/nested/util.py"}]
    assert counts["matches"] == [{"path": "src/main.py", "matches": 3}]


# 功能：Grep 跳过二进制/超大文件，并用 truncated 标记总结果上限
async def test_grep_skips_binary_and_reports_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _make_search_tree(tmp_path)
    (tmp_path / "too-large.py").write_bytes(b"target\n" + b"x" * (1024 * 1024))
    monkeypatch.setattr(grep_module, "ripgrep_path", lambda: None)

    result = await GrepTool(workspace_root=tmp_path).invoke({
        "pattern": "target",
        "limit": 1,
        "include_hidden": True,
    })
    data = _payload(result.content)

    assert data["record_count"] == 1
    assert data["truncated"] is True
    assert data["matches"][0]["path"] == ".hidden.py"


# 功能：非法正则返回 schema_error，搜索路径逃逸继续由 WorkspaceBoundary 拒绝
async def test_grep_rejects_invalid_regex_and_workspace_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grep_module, "ripgrep_path", lambda: None)
    tool = GrepTool(workspace_root=tmp_path)

    invalid = await tool.invoke({"pattern": "["})
    assert invalid.is_error
    assert invalid.error_type == "schema_error"
    with pytest.raises(PermissionError):
        await tool.invoke({"pattern": "x", "path": ".."})


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
async def test_real_ripgrep_backend_and_unicode_column(tmp_path: Path) -> None:
    _make_search_tree(tmp_path)

    glob_result = _payload((await GlobTool(workspace_root=tmp_path).invoke({
        "pattern": "**/*.py"
    })).content)
    grep_result = _payload((await GrepTool(workspace_root=tmp_path).invoke({
        "pattern": "target",
        "glob": "**/*.py",
    })).content)

    assert glob_result["backend"] == "ripgrep"
    assert glob_result["files"] == ["binary.py", "src/main.py", "src/nested/util.py"]
    assert grep_result["backend"] == "ripgrep"
    assert grep_result["matches"][1]["column"] == 4
    assert grep_result["match_count"] == 3


def test_main_agent_registry_exposes_search_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(CodeRookConfig(), runs_dir=tmp_path / ".runs")

    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))

    assert registry.get("glob") is not None
    assert registry.get("grep") is not None
    assert registry.get("edit_file") is not None
    assert registry.get("apply_patch") is not None
    assert registry.get("git_diff") is not None

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from code_rook.core.repository import (
    RepositoryIndex,
    RepositoryTool,
    command_candidate_id,
    discover_test_commands,
    render_test_command,
)
from code_rook.core.workspace import WorkspaceBoundary


# 功能：验证 Python、Node、Rust 与 Go manifest 只生成带来源的固定 argv 候选
# 设计：构造多语言 monorepo 并在 Node test script 放置副作用文本，断言发现过程不执行脚本
def test_discover_test_commands_returns_candidates_without_execution(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    frontend = tmp_path / "apps" / "前端"
    frontend.mkdir(parents=True)
    marker = tmp_path / "must-not-exist"
    (frontend / "package.json").write_text(
        json.dumps(
            {
                "packageManager": "pnpm@10.0.0",
                "scripts": {"test": f"python -c write({marker})"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rust = tmp_path / "crates" / "engine"
    rust.mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\nname='engine'\n", encoding="utf-8")
    go = tmp_path / "services" / "api"
    go.mkdir(parents=True)
    (go / "go.mod").write_text("module example.test/api\n", encoding="utf-8")

    discovery = discover_test_commands(WorkspaceBoundary(tmp_path))
    by_source = {candidate.source: candidate for candidate in discovery.candidates}

    assert by_source["pyproject.toml"].argv == ("uv", "run", "pytest")
    assert by_source["apps/前端/package.json"].argv == ("pnpm", "test")
    assert by_source["crates/engine/Cargo.toml"].argv == ("cargo", "test")
    assert by_source["services/api/go.mod"].argv == ("go", "test", "./...")
    assert by_source["apps/前端/package.json"].cwd == "apps/前端"
    assert all(candidate.trust == "manifest_declared" for candidate in by_source.values())
    expected_python = '"uv" "run" "pytest"' if os.name == "nt" else "uv run pytest"
    assert render_test_command(by_source["pyproject.toml"]) == expected_python
    assert len(command_candidate_id(by_source["pyproject.toml"])) == 64
    assert not marker.exists()


# 功能：验证命令发现遵守候选上限、忽略构建依赖目录并明确标记截断
# 设计：创建三个有效 package.json 并在 node_modules 放置诱饵，以 max_candidates=2 检查边界
def test_discover_test_commands_is_bounded_and_ignores_dependencies(
    tmp_path: Path,
) -> None:
    for name in ("a", "b", "c"):
        directory = tmp_path / "packages" / name
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(
            '{"scripts":{"test":"vitest"}}',
            encoding="utf-8",
        )
    dependency = tmp_path / "node_modules" / "unsafe"
    dependency.mkdir(parents=True)
    (dependency / "package.json").write_text(
        '{"scripts":{"test":"unsafe"}}',
        encoding="utf-8",
    )

    discovery = discover_test_commands(
        WorkspaceBoundary(tmp_path),
        max_candidates=2,
    )

    assert len(discovery.candidates) == 2
    assert discovery.truncated is True
    assert all("node_modules" not in item.source for item in discovery.candidates)


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd quoting contract")
# 功能：验证 Windows 候选目录中的 shell 元字符被强制引用且变量展开字符直接拒绝
# 设计：同时构造 ampersand 与 percent 目录，检查前者留在引号内、后者不进入候选目录
def test_windows_test_command_rendering_rejects_cmd_injection(tmp_path: Path) -> None:
    safe = tmp_path / "safe&name"
    unsafe = tmp_path / "unsafe%PATH%"
    for directory in (safe, unsafe):
        directory.mkdir()
        (directory / "package.json").write_text(
            '{"scripts":{"test":"vitest"}}',
            encoding="utf-8",
        )

    discovery = discover_test_commands(WorkspaceBoundary(tmp_path))
    by_source = {candidate.source: candidate for candidate in discovery.candidates}

    safe_candidate = by_source["safe&name/package.json"]
    assert render_test_command(safe_candidate).startswith('cd /d "safe&name" && ')
    assert "unsafe%PATH%/package.json" not in by_source


# 功能：验证 Repository 工具通过只读 action 暴露候选、来源及 executed=false 收据
# 设计：调用真实异步工具入口并解析 JSON，证明发现能力没有复用会执行命令的 Run 工具
async def test_repository_tool_exposes_non_executing_test_command_discovery(
    tmp_path: Path,
) -> None:
    (tmp_path / "go.mod").write_text("module example.test/tool\n", encoding="utf-8")
    tool = RepositoryTool(
        RepositoryIndex(
            WorkspaceBoundary(tmp_path),
            cache_path=tmp_path / ".repository-cache.json",
        )
    )

    result = await tool.invoke({"action": "test_commands", "limit": 10})
    payload = json.loads(result.content)

    assert result.is_error is False
    assert payload["executed"] is False
    assert payload["backend"] == "manifest-discovery"
    assert payload["candidates"][0]["argv"] == ["go", "test", "./..."]
    assert payload["candidates"][0]["source"] == "go.mod"
    candidate = discover_test_commands(WorkspaceBoundary(tmp_path)).candidates[0]
    assert payload["candidates"][0]["command"] == render_test_command(candidate)
    assert len(payload["candidates"][0]["candidate_id"]) == 64

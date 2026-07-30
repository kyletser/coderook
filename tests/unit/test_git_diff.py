from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from code_rook.core.tools.builtin.git_diff import GitDiffTool

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "CodeRook Test")
    _git(root, "config", "user.email", "coderook@example.invalid")


def _commit_all(root: Path, message: str = "initial") -> None:
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", message)


def _payload(content: str) -> dict:
    return json.loads(content)


async def test_git_diff_all_lists_tracked_and_untracked_without_writing_index(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("old\n", encoding="utf-8")
    _commit_all(tmp_path)
    tracked.write_text("new\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("new file\n", encoding="utf-8")
    index_path = tmp_path / ".git" / "index"
    index_before = index_path.read_bytes()

    result = await GitDiffTool(workspace_root=tmp_path).invoke({})
    data = _payload(result.content)

    assert not result.is_error
    assert [item["path"] for item in data["files"]] == ["tracked.txt", "untracked.txt"]
    tracked_info = data["files"][0]
    assert tracked_info["staged"] is False
    assert tracked_info["unstaged"] is True
    assert tracked_info["additions"] == 1
    assert tracked_info["deletions"] == 1
    assert data["files"][1]["untracked"] is True
    assert data["files"][1]["additions"] is None
    assert "-old" in data["diff"]
    assert "+new" in data["diff"]
    assert index_path.read_bytes() == index_before


async def test_git_diff_separates_staged_and_unstaged_scopes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "value.txt"
    target.write_text("base\n", encoding="utf-8")
    _commit_all(tmp_path)
    target.write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "value.txt")
    target.write_text("worktree\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("u\n", encoding="utf-8")
    tool = GitDiffTool(workspace_root=tmp_path)

    staged = _payload((await tool.invoke({"scope": "staged"})).content)
    unstaged = _payload((await tool.invoke({"scope": "unstaged"})).content)

    assert [item["path"] for item in staged["files"]] == ["value.txt"]
    assert staged["files"][0]["staged"] is True
    assert "+staged" in staged["diff"]
    assert "worktree" not in staged["diff"]
    assert [item["path"] for item in unstaged["files"]] == [
        "untracked.txt",
        "value.txt",
    ]
    assert "+worktree" in unstaged["diff"]
    assert "-staged" in unstaged["diff"]


async def test_git_diff_parses_rename_and_path_filter(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "old.txt").write_text("content\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("other\n", encoding="utf-8")
    _commit_all(tmp_path)
    _git(tmp_path, "mv", "old.txt", "new.txt")
    (tmp_path / "other.txt").write_text("changed\n", encoding="utf-8")

    tool = GitDiffTool(workspace_root=tmp_path)
    result = await tool.invoke({})
    data = _payload(result.content)

    assert not result.is_error
    rename = next(item for item in data["files"] if item["path"] == "new.txt")
    assert rename["original_path"] == "old.txt"

    filtered = _payload((await tool.invoke({"path": "other.txt"})).content)
    assert [item["path"] for item in filtered["files"]] == ["other.txt"]
    assert "new.txt" not in filtered["diff"]


async def test_git_diff_reports_bounded_output(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "large.txt"
    target.write_text("".join(f"old {index}\n" for index in range(500)), encoding="utf-8")
    _commit_all(tmp_path)
    target.write_text("".join(f"new {index}\n" for index in range(500)), encoding="utf-8")

    result = await GitDiffTool(workspace_root=tmp_path).invoke({"diff_limit": 1000})
    data = _payload(result.content)

    assert not result.is_error
    assert data["diff_truncated"] is True
    assert data["diff"].endswith("[diff truncated]\n")
    assert len(data["diff"].encode("utf-8")) < 1100


async def test_git_diff_rejects_non_repository(tmp_path: Path) -> None:
    result = await GitDiffTool(workspace_root=tmp_path).invoke({})

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "not_git_repository"


async def test_git_diff_rejects_repository_root_outside_workspace(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = tmp_path / "subdir"
    workspace.mkdir()

    result = await GitDiffTool(workspace_root=workspace).invoke({})

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "repository_outside_workspace"


async def test_git_diff_handles_unborn_repository(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "new.txt"
    target.write_text("staged\n", encoding="utf-8")
    _git(tmp_path, "add", "new.txt")

    result = await GitDiffTool(workspace_root=tmp_path).invoke({})
    data = _payload(result.content)

    assert not result.is_error
    assert data["has_head"] is False
    assert data["files"][0]["path"] == "new.txt"
    assert "+staged" in data["diff"]

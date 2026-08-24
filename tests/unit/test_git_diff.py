from __future__ import annotations

import hashlib
import json
import os
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
    assert data["files"][1]["additions"] == 1
    assert data["files"][1]["review_complete"] is True
    assert data["files"][1]["review_status"] == "text"
    assert "diff --git a/untracked.txt b/untracked.txt" in data["diff"]
    assert "+new file" in data["diff"]
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


# 功能：子目录 workspace 的结构化路径与原始 diff 都严格使用 workspace 相对名称
# 设计：提交含空格和 Unicode 的子目录文件后同时修改内外文件，核对公开路径和 header 不泄漏仓库前缀
async def test_git_diff_allows_monorepo_subdirectory_workspace(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = tmp_path / "packages" / "app"
    workspace.mkdir(parents=True)
    target = workspace / "inner space-中文.txt"
    outside = tmp_path / "outside.txt"
    target.write_text("before\n", encoding="utf-8")
    outside.write_text("before\n", encoding="utf-8")
    _commit_all(tmp_path)
    target.write_text("scoped\n", encoding="utf-8")
    outside.write_text("outside\n", encoding="utf-8")

    result = await GitDiffTool(workspace_root=workspace).invoke({})
    data = _payload(result.content)

    assert not result.is_error
    paths = [file["path"] for file in data["files"]]
    assert paths == ["inner space-中文.txt"]
    assert "outside.txt" not in paths
    assert "diff --git a/inner space-中文.txt b/inner space-中文.txt" in data["diff"]
    assert "packages/app" not in data["diff"]


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


# 功能：未跟踪二进制文件只返回完整长度与摘要，不把原始控制字节写入文本 diff
# 设计：使用含 NUL 的小文件区分文本路径，断言审查完整但展示保持有界且 index 不变
async def test_git_diff_represents_untracked_binary_with_safe_metadata(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _commit_all(tmp_path)
    binary = tmp_path / "asset.bin"
    binary.write_bytes(b"\x00secret-binary\xff")
    index_before = (tmp_path / ".git" / "index").read_bytes()

    result = await GitDiffTool(workspace_root=tmp_path).invoke({})
    data = _payload(result.content)
    file_info = data["files"][0]

    assert not result.is_error
    assert file_info["path"] == "asset.bin"
    assert file_info["review_status"] == "binary"
    assert file_info["review_complete"] is True
    assert file_info["content_size"] == len(binary.read_bytes())
    assert len(file_info["content_sha256"]) == 64
    assert "secret-binary" not in data["diff"]
    assert (tmp_path / ".git" / "index").read_bytes() == index_before


# 功能：超过可见预算的未跟踪文本明确标记为截断且不能伪装成完整审查
# 设计：把单文件正文设为 diff_limit 两倍，验证工具不返回正文并保留大小和阻断状态
async def test_git_diff_blocks_oversized_untracked_text_review(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    target = tmp_path / "large.txt"
    target.write_text("x" * 2500, encoding="utf-8")

    result = await GitDiffTool(workspace_root=tmp_path).invoke({"diff_limit": 1000})
    data = _payload(result.content)
    file_info = data["files"][0]

    assert not result.is_error
    assert file_info["review_status"] == "truncated"
    assert file_info["review_complete"] is False
    assert file_info["content_size"] == 2500
    assert file_info["content_sha256"] is None
    assert data["diff_truncated"] is True
    assert "x" * 100 not in data["diff"]


# 功能：纯 rename 的 Git 元数据完整进入可见 diff 且文件记录保持可审查
# 设计：提交单文件后用 git mv 产生零正文变更，直接核对 similarity 与 rename 前后路径
async def test_git_diff_preserves_pure_rename_metadata(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "before.txt").write_text("same\n", encoding="utf-8")
    _commit_all(tmp_path)
    _git(tmp_path, "mv", "before.txt", "after.txt")

    data = _payload((await GitDiffTool(workspace_root=tmp_path).invoke({})).content)
    file_info = data["files"][0]

    assert file_info["original_path"] == "before.txt"
    assert file_info["path"] == "after.txt"
    assert file_info["review_complete"] is True
    assert "similarity index 100%" in data["diff"]
    assert "rename from before.txt" in data["diff"]
    assert "rename to after.txt" in data["diff"]


# 功能：mode-only 变更不依赖文本 hunk 也能保留完整 old/new mode 证据
# 设计：用 update-index 跨平台修改可执行位，只审查 staged 范围并断言两个 mode 行可见
async def test_git_diff_preserves_mode_only_metadata(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "script.sh").write_text("echo safe\n", encoding="utf-8")
    _commit_all(tmp_path)
    _git(tmp_path, "update-index", "--chmod=+x", "script.sh")

    data = _payload(
        (await GitDiffTool(workspace_root=tmp_path).invoke({"scope": "staged"})).content
    )

    assert data["files"][0]["review_complete"] is True
    assert "old mode 100644" in data["diff"]
    assert "new mode 100755" in data["diff"]
    assert "@@" not in data["diff"]


# 功能：tracked binary 只有在 old/new 完整大小与 SHA-256 可见时才完成审查
# 设计：提交并修改含 NUL 的小 blob，按原始字节计算摘要并核对文件字段与可见证据块
async def test_git_diff_proves_tracked_binary_with_old_and_new_hashes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    old = b"\x00old-binary\xff"
    new = b"\x00new-binary\xfe"
    target = tmp_path / "asset.bin"
    target.write_bytes(old)
    _commit_all(tmp_path)
    target.write_bytes(new)

    data = _payload((await GitDiffTool(workspace_root=tmp_path).invoke({})).content)
    file_info = data["files"][0]

    assert file_info["review_status"] == "binary"
    assert file_info["review_complete"] is True
    assert file_info["old_content_size"] == len(old)
    assert file_info["old_content_sha256"] == hashlib.sha256(old).hexdigest()
    assert file_info["new_content_size"] == len(new)
    assert file_info["new_content_sha256"] == hashlib.sha256(new).hexdigest()
    assert "CodeRook opaque blob evidence" in data["diff"]
    assert file_info["old_content_sha256"] in data["diff"]
    assert file_info["new_content_sha256"] in data["diff"]


# 功能：tracked 非 UTF-8 文本不得因 errors=replace 而被标记为完整文本审查
# 设计：使用无 NUL 的非法 UTF-8 行触发普通 Git patch，再要求 old/new blob 摘要成为权威证据
async def test_git_diff_marks_non_utf8_tracked_patch_as_opaque(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    old = b"\xffold-line\n"
    new = b"\xfenew-line\n"
    target = tmp_path / "legacy.txt"
    target.write_bytes(old)
    _commit_all(tmp_path)
    target.write_bytes(new)

    data = _payload((await GitDiffTool(workspace_root=tmp_path).invoke({})).content)
    file_info = data["files"][0]

    assert file_info["additions"] == 1
    assert file_info["deletions"] == 1
    assert file_info["review_status"] == "opaque"
    assert file_info["review_complete"] is True
    assert file_info["old_content_sha256"] == hashlib.sha256(old).hexdigest()
    assert file_info["new_content_sha256"] == hashlib.sha256(new).hexdigest()
    assert "CodeRook opaque blob evidence" in data["diff"]


# 功能：含空格路径的非法 UTF-8 tracked patch 不能因 header 歧义逃过不透明证据审查
# 设计：让原生未引号 diff header 无法用空格拆词，断言工具保守降级并仍绑定完整前后摘要
async def test_git_diff_marks_non_utf8_patch_with_space_path_as_opaque(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    old = b"\xffold-line\n"
    new = b"\xfenew-line\n"
    target = tmp_path / "legacy file.txt"
    target.write_bytes(old)
    _commit_all(tmp_path)
    target.write_bytes(new)

    data = _payload((await GitDiffTool(workspace_root=tmp_path).invoke({})).content)
    file_info = data["files"][0]

    assert file_info["path"] == "legacy file.txt"
    assert file_info["review_status"] == "opaque"
    assert file_info["review_complete"] is True
    assert file_info["old_content_sha256"] == hashlib.sha256(old).hexdigest()
    assert file_info["new_content_sha256"] == hashlib.sha256(new).hexdigest()
    assert "CodeRook opaque blob evidence" in data["diff"]


# 功能：POSIX 字面反斜杠文件名不会与同名目录路径折叠成同一个 files[].path
# 设计：同时修改 `a\\b.txt` 与 `a/b.txt`，用 Git NUL 路径输出验证两个身份完整保留
@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot contain backslash")
async def test_git_diff_preserves_literal_backslash_path_identity(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    literal = tmp_path / r"a\b.txt"
    nested = tmp_path / "a" / "b.txt"
    nested.parent.mkdir()
    literal.write_text("before literal\n", encoding="utf-8")
    nested.write_text("before nested\n", encoding="utf-8")
    _commit_all(tmp_path)
    literal.write_text("after literal\n", encoding="utf-8")
    nested.write_text("after nested\n", encoding="utf-8")

    data = _payload((await GitDiffTool(workspace_root=tmp_path).invoke({})).content)

    assert {file_info["path"] for file_info in data["files"]} == {
        r"a\b.txt",
        "a/b.txt",
    }


# 功能：多个 tracked opaque 证据超出总可见预算时必须整体失败关闭
# 设计：让原始 binary metadata 能放入最小预算但全部摘要不能，断言至少一项阻断且输出仍有界
async def test_git_diff_fails_closed_when_opaque_evidence_exceeds_budget(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    targets = [tmp_path / f"asset_{index}.bin" for index in range(6)]
    for index, target in enumerate(targets):
        target.write_bytes(b"\x00old" + bytes([index]))
    _commit_all(tmp_path)
    for index, target in enumerate(targets):
        target.write_bytes(b"\x00new" + bytes([index]))

    data = _payload(
        (await GitDiffTool(workspace_root=tmp_path).invoke({"diff_limit": 1000})).content
    )

    assert data["diff_truncated"] is True
    assert any(file_info["review_complete"] is False for file_info in data["files"])
    assert len(data["diff"].encode()) < 1100

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from rich.text import Text

from code_rook.core.change_center import ChangeCenterService
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.tui.panels import (
    ChangeCenterPanel,
    build_change_snapshot,
    parse_unified_diff,
)


# 构造包含两个文件和三个 hunk 的统一 diff 测试事实
def _sample_diff() -> str:
    return """diff --git a/src/a.py b/src/a.py
index 1111111..2222222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,2 +1,3 @@ def first():
-    return 1
+    value = 2
+    return value
@@ -10 +11 @@ def second():
-    return False
+    return True
diff --git "a/docs/read me.md" "b/docs/read me.md"
index 3333333..4444444 100644
--- "a/docs/read me.md"
+++ "b/docs/read me.md"
@@ -1 +1 @@
-old
+new
"""


# 构造与统一 diff 对齐的 workspace.diff IPC 包装结果
def _diff_result() -> dict[str, object]:
    return {
        "payload": {
            "scope": "all",
            "files": [
                {
                    "path": "src/a.py",
                    "original_path": None,
                    "index_status": " ",
                    "worktree_status": "M",
                    "staged": False,
                    "unstaged": True,
                    "untracked": False,
                    "additions": 3,
                    "deletions": 2,
                },
                {
                    "path": "docs/read me.md",
                    "original_path": None,
                    "index_status": "M",
                    "worktree_status": " ",
                    "staged": True,
                    "unstaged": False,
                    "untracked": False,
                    "additions": 1,
                    "deletions": 1,
                },
            ],
            "file_count": 2,
            "additions": 4,
            "deletions": 3,
            "diff": _sample_diff(),
            "diff_truncated": False,
            "state_digest": "a" * 64,
        }
    }


# 构造逐文件覆盖且包含真实命令的 Turn Receipt 验证事实
def _receipt() -> dict[str, object]:
    return {
        "receipt": {
            "verification": [
                {
                    "tool": "Run",
                    "action": "verifiers",
                    "passed": 1,
                    "failed": 0,
                    "paths": ["src/a.py"],
                    "gates": [
                        {
                            "name": "pytest",
                            "command": "uv run pytest tests/unit/test_a.py",
                            "status": "passed",
                            "duration_ms": 42,
                        }
                    ],
                },
                {
                    "tool": "markdownlint",
                    "status": "ok",
                    "paths": ["docs/read me.md"],
                    "duration_ms": 3,
                },
            ],
            "unavailable": [],
        }
    }


# 功能：统一 diff 解析保留文件边界、含空格路径、范围和每个 hunk 的内容
# 设计：使用双文件三 hunk fixture 直接断言语义字段，避免用渲染文本掩盖解析错误
def test_parse_unified_diff_preserves_file_and_hunk_metadata() -> None:
    hunks = parse_unified_diff(_sample_diff())

    assert len(hunks) == 3
    assert [hunk.file_path for hunk in hunks] == [
        "src/a.py",
        "src/a.py",
        "docs/read me.md",
    ]
    assert (hunks[0].old_start, hunks[0].old_count) == (1, 2)
    assert (hunks[0].new_start, hunks[0].new_count) == (1, 3)
    assert hunks[0].section == "def first():"
    assert "+    value = 2" in hunks[0].lines


# 功能：快照把 workspace.diff 文件统计和 receipt gate 映射为可信完成状态
# 设计：两个变更文件分别由命令 gate 与 diagnostics 覆盖，证明路径覆盖而非“有测试即全绿”
def test_snapshot_maps_verification_commands_to_changed_files() -> None:
    snapshot = build_change_snapshot(_diff_result(), _receipt())

    assert snapshot.additions == 4
    assert snapshot.deletions == 3
    assert snapshot.state_digest == "a" * 64
    assert snapshot.fully_verified is True
    assert snapshot.unverified_paths == ()
    assert snapshot.verifications[0].command == "uv run pytest tests/unit/test_a.py"
    assert snapshot.verifications[0].paths == ("src/a.py",)
    assert snapshot.verifications[1].name == "markdownlint"


# 功能：hunk 上下导航跨文件时同步文件选择且在首尾边界失败关闭
# 设计：从默认首 hunk 连续前进到第三个 hunk，核对文件切换和无循环边界语义
def test_panel_hunk_navigation_updates_current_file_without_wrapping() -> None:
    panel = ChangeCenterPanel()
    panel.update(_diff_result(), _receipt())

    assert panel.current_file is not None
    assert panel.current_file.path == "docs/read me.md"
    assert panel.current_hunk is not None
    assert panel.current_hunk.file_path == "docs/read me.md"
    assert panel.previous_hunk() is True
    assert panel.current_file is not None
    assert panel.current_file.path == "src/a.py"
    assert panel.previous_hunk() is True
    assert panel.previous_hunk() is False
    assert panel.next_hunk() is True
    assert panel.next_hunk() is True
    assert panel.next_hunk() is False
    assert panel.current_file is not None
    assert panel.current_file.path == "docs/read me.md"


# 功能：选中无文本 hunk 的二进制文件时清空旧 hunk 而不展示其他文件内容
# 设计：追加仅出现在结构化文件清单的 binary 记录，验证文件选择和 diff 选择严格关联
def test_panel_select_binary_file_clears_unrelated_hunk() -> None:
    result = _diff_result()
    payload = result["payload"]
    assert isinstance(payload, dict)
    files = payload["files"]
    assert isinstance(files, list)
    files.append(
        {
            "path": "assets/logo.bin",
            "index_status": " ",
            "worktree_status": "M",
            "unstaged": True,
            "additions": None,
            "deletions": None,
        }
    )
    panel = ChangeCenterPanel()
    panel.update(result, _receipt())

    assert panel.select_file("docs/read me.md") is True
    assert panel.select_file("assets/logo.bin") is True
    assert panel.current_file is not None
    assert panel.current_file.path == "assets/logo.bin"
    assert panel.current_hunk is None
    assert "No textual hunk" in panel.render(width=80, height=24)


# 功能：冲突、失败验证、未覆盖路径和截断 diff 都以阻断提示呈现
# 设计：组合 UU 冲突与 failed gate 并开启截断，确保危险状态不会被成功 gate 或统计掩盖
def test_panel_renders_conflict_failure_and_truncation_warnings() -> None:
    result = _diff_result()
    payload = result["payload"]
    assert isinstance(payload, dict)
    files = payload["files"]
    assert isinstance(files, list)
    first = files[0]
    assert isinstance(first, dict)
    first["index_status"] = "U"
    first["worktree_status"] = "U"
    payload["diff_truncated"] = True
    receipt = {
        "verification": [
            {
                "action": "tests",
                "failed": 1,
                "paths": ["src/a.py"],
                "gates": [{"name": "pytest", "status": "failed"}],
                "failure_class": "test_failure",
            }
        ],
        "unavailable": [],
    }
    panel = ChangeCenterPanel()
    panel.update(result, receipt)

    rendered = panel.render(width=80, height=24)
    assert "Conflicts block completion" in rendered
    assert "Verification failed" in rendered
    assert "Diff truncated" in rendered
    assert panel.snapshot.fully_verified is False
    assert panel.snapshot.conflict_paths == ("src/a.py",)


# 功能：80×24 窄终端保留文件、当前 hunk、验证状态且不超过高度预算
# 设计：用 Rich 实际解析 markup 后检查每行 cell 宽度，覆盖仓库可控括号文本的安全转义
def test_panel_render_fits_narrow_terminal_and_escapes_paths() -> None:
    result = _diff_result()
    payload = result["payload"]
    assert isinstance(payload, dict)
    files = payload["files"]
    assert isinstance(files, list)
    first = files[0]
    assert isinstance(first, dict)
    first["path"] = "src/[bold]not-markup.py"
    panel = ChangeCenterPanel()
    panel.update(result, _receipt())

    rendered = panel.render(width=80, height=24)
    lines = rendered.splitlines()
    assert len(lines) <= 24
    assert all(Text.from_markup(line).cell_len <= 80 for line in lines)
    assert "[bold]not-markup.py" in Text.from_markup(rendered).plain
    assert "Change Center" in rendered
    assert "Diff" in rendered
    assert "Verification" in rendered


# 功能：无 receipt 时面板明确标记未验证，但空工作区不会产生虚假验证警告
# 设计：分别渲染有变更和空 payload，固定“缺证据”只针对实际改动的产品语义
def test_panel_distinguishes_unverified_changes_from_empty_workspace() -> None:
    panel = ChangeCenterPanel()
    panel.update(_diff_result())
    assert "Unverified: no durable verification receipt" in panel.render()

    panel.update({"payload": {"files": [], "diff": "", "scope": "all"}})
    rendered = panel.render(width=80, height=24)
    assert "No workspace changes" in rendered
    assert "Unverified" not in rendered


# 功能：未跟踪 UTF-8 新文件的 synthetic unified diff 在 Change Center 中可见且可导航
# 设计：构造带完整审查字段的新文件 payload，直接断言 hunk 归属和正文渲染
def test_panel_renders_untracked_text_body_as_reviewable_hunk() -> None:
    diff = """diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+print("visible")
+value = 1
"""
    result = {
        "payload": {
            "scope": "all",
            "files": [
                {
                    "path": "new.py",
                    "index_status": "?",
                    "worktree_status": "?",
                    "untracked": True,
                    "unstaged": True,
                    "additions": 2,
                    "deletions": 0,
                    "review_status": "text",
                    "review_complete": True,
                    "review_note": "Full UTF-8 text review",
                }
            ],
            "diff": diff,
            "diff_truncated": False,
        }
    }
    panel = ChangeCenterPanel()
    panel.update(result)

    assert panel.current_hunk is not None
    assert panel.current_hunk.file_path == "new.py"
    rendered = Text.from_markup(panel.render(width=80, height=24)).plain
    assert 'print("visible")' in rendered
    assert "value = 1" in rendered
    assert panel.snapshot.unreviewable_paths == ()


# 功能：二进制与超大未跟踪文件在面板中显示安全摘要或明确审查阻断
# 设计：分别注入完整二进制摘要和截断状态，验证不伪造文本 hunk且风险可见
def test_panel_renders_binary_metadata_and_oversized_review_block() -> None:
    digest = "b" * 64
    result = {
        "payload": {
            "scope": "all",
            "files": [
                {
                    "path": "asset.bin",
                    "index_status": "?",
                    "worktree_status": "?",
                    "untracked": True,
                    "unstaged": True,
                    "review_status": "binary",
                    "review_complete": True,
                    "review_note": f"Binary content review: 12 bytes; SHA-256 {digest}",
                    "content_size": 12,
                    "content_sha256": digest,
                },
                {
                    "path": "large.txt",
                    "index_status": "?",
                    "worktree_status": "?",
                    "untracked": True,
                    "unstaged": True,
                    "review_status": "truncated",
                    "review_complete": False,
                    "review_note": "Review blocked: file exceeds visible diff limit",
                    "content_size": 300000,
                },
            ],
            "diff": "",
            "diff_truncated": True,
        }
    }
    panel = ChangeCenterPanel()
    panel.update(result)

    assert panel.select_file("asset.bin") is False
    binary_rendered = Text.from_markup(panel.render(width=100, height=24)).plain
    assert "Binary content review" in binary_rendered
    assert digest in binary_rendered.replace("\n", "")
    assert panel.select_file("large.txt") is True
    blocked_rendered = Text.from_markup(panel.render(width=100, height=24)).plain
    assert "Visible review required before stage/commit" in blocked_rendered
    assert "Review blocked" in blocked_rendered
    assert panel.snapshot.unreviewable_paths == ("large.txt",)


# 功能：超长新增行可通过 PageDown 分页看到尾部而不是被终端宽度永久裁剪
# 设计：用单个远超 40 列的新增行反复翻页，断言最终页面包含唯一尾部标记
def test_panel_paginates_wrapped_diff_rows_without_losing_line_tail() -> None:
    long_line = "+" + "x" * 180 + "TAIL"
    diff = (
        "diff --git a/new.txt b/new.txt\n"
        "--- /dev/null\n"
        "+++ b/new.txt\n"
        "@@ -0,0 +1 @@\n"
        f"{long_line}\n"
    )
    result = {
        "payload": {
            "scope": "all",
            "files": [
                {
                    "path": "new.txt",
                    "index_status": "?",
                    "worktree_status": "?",
                    "untracked": True,
                    "unstaged": True,
                    "review_status": "text",
                    "review_complete": True,
                }
            ],
            "diff": diff,
            "diff_truncated": False,
        }
    }
    panel = ChangeCenterPanel()
    panel.update(result)
    rendered_pages = [Text.from_markup(panel.render(width=40, height=12)).plain]

    while panel.next_page():
        rendered_pages.append(Text.from_markup(panel.render(width=40, height=12)).plain)

    assert len(rendered_pages) >= 2
    assert "TAIL" in rendered_pages[-1]


# 功能：Change Center 按文件保留并展示 rename、copy、mode-only 与 binary 元数据块
# 设计：构造四个均无 @@ hunk 的连续 diff，逐文件选择并断言不会回退到其他文件或丢失 Git 元数据
def test_panel_navigates_and_renders_metadata_only_file_blocks() -> None:
    old_digest = "1" * 64
    new_digest = "2" * 64
    diff = f"""diff --git a/old.txt b/renamed.txt
similarity index 100%
rename from old.txt
rename to renamed.txt
diff --git a/source.txt b/copied.txt
similarity index 100%
copy from source.txt
copy to copied.txt
diff --git a/script.sh b/script.sh
old mode 100644
new mode 100755
diff --git a/asset.bin b/asset.bin
index 1111111..2222222 100644
Binary files a/asset.bin and b/asset.bin differ
diff --git a/asset.bin b/asset.bin
CodeRook opaque blob evidence
old blob: 12 bytes SHA-256 {old_digest}
new blob: 13 bytes SHA-256 {new_digest}
"""
    files = [
        {
            "path": "renamed.txt",
            "original_path": "old.txt",
            "index_status": "R",
            "worktree_status": " ",
            "review_status": "text",
            "review_complete": True,
        },
        {
            "path": "copied.txt",
            "original_path": "source.txt",
            "index_status": "C",
            "worktree_status": " ",
            "review_status": "text",
            "review_complete": True,
        },
        {
            "path": "script.sh",
            "index_status": "M",
            "worktree_status": " ",
            "review_status": "text",
            "review_complete": True,
        },
        {
            "path": "asset.bin",
            "index_status": " ",
            "worktree_status": "M",
            "review_status": "opaque",
            "review_complete": True,
            "old_content_present": True,
            "old_content_size": 12,
            "old_content_sha256": old_digest,
            "new_content_present": True,
            "new_content_size": 13,
            "new_content_sha256": new_digest,
        },
    ]
    panel = ChangeCenterPanel()
    panel.update({"payload": {"files": files, "diff": diff, "diff_truncated": False}})

    expected = {
        "renamed.txt": ("rename from old.txt", "rename to renamed.txt"),
        "copied.txt": ("copy from source.txt", "copy to copied.txt"),
        "script.sh": ("old mode 100644", "new mode 100755"),
        "asset.bin": ("Binary files", new_digest),
    }
    for path, markers in expected.items():
        panel.select_file(path)
        assert panel.current_hunk is None
        rendered = Text.from_markup(panel.render(width=100, height=24)).plain.replace("\n", "")
        assert all(marker in rendered for marker in markers)
        assert "No textual hunk" in rendered

    assert panel.snapshot.files[0].old_content_sha256 == old_digest
    assert panel.snapshot.files[0].new_content_sha256 == new_digest


# 功能：空格、Tab、引号、Unicode 与 rename header 均精确归属到 files[].path
# 设计：混合原生未引号路径和 Git C 引号路径，逐项核对 hunk/metadata 且禁止解析器截断文件名
def test_snapshot_matches_special_git_paths_without_losing_identity() -> None:
    cases = (
        ("space file.txt", "a/space file.txt", "b/space file.txt", "+space"),
        ("tab\tfile.txt", '"a/tab\\tfile.txt"', '"b/tab\\tfile.txt"', "+tab"),
        ('quote"file.txt', '"a/quote\\"file.txt"', '"b/quote\\"file.txt"', "+quote"),
        (
            r"back\slash.txt",
            '"a/back\\\\slash.txt"',
            '"b/back\\\\slash.txt"',
            "+backslash",
        ),
        ("目录/文件.py", "a/目录/文件.py", "b/目录/文件.py", "+unicode"),
    )
    blocks = [
        (
            f"diff --git {old_token} {new_token}\n"
            f"--- {old_token}\n"
            f"+++ {new_token}\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            f"{added}\n"
        )
        for _, old_token, new_token, added in cases
    ]
    blocks.append(
        "diff --git a/old name.txt b/new name.txt\n"
        "similarity index 100%\n"
        "rename from old name.txt\n"
        "rename to new name.txt\n"
    )
    files = [
        {
            "path": path,
            "index_status": " ",
            "worktree_status": "M",
            "review_status": "text",
            "review_complete": True,
        }
        for path, _, _, _ in cases
    ]
    files.append(
        {
            "path": "new name.txt",
            "original_path": "old name.txt",
            "index_status": "R",
            "worktree_status": " ",
            "review_status": "text",
            "review_complete": True,
        }
    )

    snapshot = build_change_snapshot(
        {"payload": {"files": files, "diff": "".join(blocks)}}
    )

    expected = {path for path, _, _, _ in cases}
    assert {hunk.file_path for hunk in snapshot.hunks} == expected
    assert {record.file_path for record in snapshot.metadata} == expected | {
        "new name.txt"
    }
    assert snapshot.unreviewable_paths == ()


# 功能：首尾空格文件名不会在 TUI 解析时折叠为普通名称或彼此碰撞
# 设计：同时提供三个仅边界空格不同的真实路径，并用 Git header 终止 Tab 验证精确身份映射
def test_snapshot_preserves_leading_and_trailing_space_path_identity() -> None:
    paths = ("name.txt", " name.txt", "name.txt ")
    blocks = [
        (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\t\n"
            f"+++ b/{path}\t\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        for path in paths
    ]
    files = [
        {
            "path": path,
            "index_status": " ",
            "worktree_status": "M",
            "review_status": "text",
            "review_complete": True,
        }
        for path in paths
    ]

    snapshot = build_change_snapshot(
        {"payload": {"files": files, "diff": "".join(blocks)}}
    )

    assert {item.path for item in snapshot.files} == set(paths)
    assert {hunk.file_path for hunk in snapshot.hunks} == set(paths)
    assert snapshot.unreviewable_paths == ()


# 功能：无法绑定到结构化文件路径的正文不会继续保持 review_complete
# 设计：让 payload 文件与完整 patch header 故意错位，断言快照失败关闭而非显示空正文后放行
def test_snapshot_fails_closed_when_diff_path_cannot_be_matched() -> None:
    snapshot = build_change_snapshot(
        {
            "payload": {
                "files": [
                    {
                        "path": "expected name.txt",
                        "index_status": " ",
                        "worktree_status": "M",
                        "review_status": "text",
                        "review_complete": True,
                    }
                ],
                "diff": (
                    "diff --git a/other.txt b/other.txt\n"
                    "--- a/other.txt\n"
                    "+++ b/other.txt\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            }
        }
    )

    assert snapshot.unreviewable_paths == ("expected name.txt",)
    assert "could not be matched" in snapshot.files[0].review_note


# 初始化包含 rename、mode、binary 与文本基线的子目录仓库
def _init_subdirectory_change_center_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "CodeRook Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "config",
            "user.email",
            "coderook@example.invalid",
        ],
        check=True,
    )
    workspace = root / "packages" / "app"
    workspace.mkdir(parents=True)
    (workspace / "text file-中文.txt").write_text("before\n", encoding="utf-8")
    (workspace / "before name.txt").write_text("rename\n", encoding="utf-8")
    (workspace / "script.sh").write_text("echo safe\n", encoding="utf-8")
    (workspace / "asset.bin").write_bytes(b"\x00old-binary\xff")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "base"],
        check=True,
    )
    return workspace


# 功能：ChangeCenterService 子目录审查可端到端交给面板展示全部精确路径
# 设计：真实 Git 同时产生文本、纯 rename、mode-only 和 binary，核对 service files 与面板证据一一对应
@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_subdirectory_change_center_service_feeds_exact_panel_paths(
    tmp_path: Path,
) -> None:
    workspace = _init_subdirectory_change_center_repo(tmp_path)
    (workspace / "text file-中文.txt").write_text("after\n", encoding="utf-8")
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "mv",
            "packages/app/before name.txt",
            "packages/app/after name.txt",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "update-index",
            "--chmod=+x",
            "packages/app/script.sh",
        ],
        check=True,
    )
    (workspace / "asset.bin").write_bytes(b"\x00new-binary\xfe")
    payload = await ChangeCenterService(WorkspaceBoundary(workspace)).diff()
    panel = ChangeCenterPanel()

    panel.update(payload)

    expected = {
        "after name.txt",
        "asset.bin",
        "script.sh",
        "text file-中文.txt",
    }
    assert {item.path for item in panel.snapshot.files} == expected
    assert {hunk.file_path for hunk in panel.snapshot.hunks} == {
        "text file-中文.txt"
    }
    assert {record.file_path for record in panel.snapshot.metadata} == expected
    assert panel.snapshot.unreviewable_paths == ()
    assert "packages/app" not in str(payload["diff"])
    markers = {
        "after name.txt": "rename to after name.txt",
        "asset.bin": "CodeRook opaque blob evidence",
        "script.sh": "new mode 100755",
        "text file-中文.txt": "+after",
    }
    for path, marker in markers.items():
        panel.select_file(path)
        assert panel.current_file is not None
        assert panel.current_file.path == path
        rendered = Text.from_markup(panel.render(width=120, height=30)).plain
        assert marker in rendered.replace("\n", "")

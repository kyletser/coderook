from __future__ import annotations

import json
from pathlib import Path

import pytest

import code_rook.core.editing.transaction as transaction_module
from code_rook.core.tools.builtin.apply_patch import ApplyPatchTool


def _payload(content: str) -> dict:
    return json.loads(content)


async def test_apply_patch_adds_modifies_and_deletes_atomically(tmp_path: Path) -> None:
    (tmp_path / "app.txt").write_text("alpha\nold\ntail\n", encoding="utf-8")
    (tmp_path / "delete.txt").write_text("gone\n", encoding="utf-8")
    patch = """\
--- a/app.txt
+++ b/app.txt
@@ -1,3 +1,3 @@
 alpha
-old
+new
 tail
--- /dev/null
+++ b/nested/new.txt
@@ -0,0 +1,2 @@
+one
+two
--- a/delete.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})
    data = _payload(result.content)

    assert not result.is_error
    assert (tmp_path / "app.txt").read_text(encoding="utf-8") == "alpha\nnew\ntail\n"
    assert (tmp_path / "nested" / "new.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert not (tmp_path / "delete.txt").exists()
    assert data["file_count"] == 3
    assert [item["action"] for item in data["files"]] == ["modify", "add", "delete"]
    assert data["additions"] == 3
    assert data["removals"] == 2


async def test_hunk_failure_in_later_file_leaves_every_file_unchanged(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("actual second\n", encoding="utf-8")
    patch = """\
--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-old first
+new first
--- a/second.txt
+++ b/second.txt
@@ -1 +1 @@
-expected second
+new second
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})
    error = _payload(result.content)["error"]

    assert result.is_error
    assert result.error_type == "conflict"
    assert error["code"] == "hunk_mismatch"
    assert error["path"] == "second.txt"
    assert error["hunk"] == 1
    assert error["expected"] == "expected second"
    assert error["actual"] == "actual second"
    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "actual second\n"


async def test_apply_patch_dry_run_only_validates(tmp_path: Path) -> None:
    target = tmp_path / "item.txt"
    target.write_text("old\n", encoding="utf-8")
    patch = """\
--- a/item.txt
+++ b/item.txt
@@ -1 +1 @@
-old
+new
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({
        "patch": patch,
        "dry_run": True,
    })

    assert not result.is_error
    assert _payload(result.content)["dry_run"] is True
    assert target.read_text(encoding="utf-8") == "old\n"


async def test_commit_failure_rolls_back_previously_installed_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    patch = """\
--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-old first
+new first
--- a/second.txt
+++ b/second.txt
@@ -1 +1 @@
-old second
+new second
"""
    real_replace = transaction_module.os.replace
    calls = 0

    def fail_third_replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("simulated commit failure")
        real_replace(source, target)

    monkeypatch.setattr(transaction_module.os, "replace", fail_third_replace)
    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "commit_failed"
    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "old second\n"
    assert list(tmp_path.glob(".*.tmp")) == []
    assert list(tmp_path.glob(".*.bak")) == []


async def test_concurrent_change_before_commit_aborts_all_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("old first\n", encoding="utf-8")
    second.write_text("old second\n", encoding="utf-8")
    patch = """\
--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-old first
+new first
--- a/second.txt
+++ b/second.txt
@@ -1 +1 @@
-old second
+new second
"""
    real_assert_current = transaction_module._assert_current
    calls = 0

    def change_before_second_check(mutations: list[transaction_module.FileMutation]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            second.write_text("user changed second\n", encoding="utf-8")
        real_assert_current(mutations)

    monkeypatch.setattr(transaction_module, "_assert_current", change_before_second_check)
    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "concurrent_change"
    assert first.read_text(encoding="utf-8") == "old first\n"
    assert second.read_text(encoding="utf-8") == "user changed second\n"
    assert list(tmp_path.glob(".*.tmp")) == []


async def test_patch_preserves_crlf_and_utf8_bom(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nold\r\n")
    patch = """\
--- a/windows.txt
+++ b/windows.txt
@@ -1,2 +1,2 @@
 first
-old
+new
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})

    assert not result.is_error
    assert target.read_bytes() == b"\xef\xbb\xbffirst\r\nnew\r\n"


async def test_patch_preserves_missing_final_newline(tmp_path: Path) -> None:
    target = tmp_path / "no-newline.txt"
    target.write_bytes(b"old")
    patch = """\
--- a/no-newline.txt
+++ b/no-newline.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})

    assert not result.is_error
    assert target.read_bytes() == b"new"


async def test_zero_length_hunk_inserts_after_source_line(tmp_path: Path) -> None:
    target = tmp_path / "insert.txt"
    target.write_text("first\nsecond\n", encoding="utf-8")
    patch = """\
--- a/insert.txt
+++ b/insert.txt
@@ -1,0 +2 @@
+inserted
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "first\ninserted\nsecond\n"


async def test_apply_patch_rejects_workspace_escape(tmp_path: Path) -> None:
    patch = """\
--- /dev/null
+++ b/../outside.txt
@@ -0,0 +1 @@
+blocked
"""

    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": patch})

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "outside_workspace"


async def test_apply_patch_rejects_invalid_diff(tmp_path: Path) -> None:
    result = await ApplyPatchTool(workspace_root=tmp_path).invoke({"patch": "not a diff"})

    assert result.is_error
    assert _payload(result.content)["error"]["code"] in {"invalid_patch", "empty_patch"}

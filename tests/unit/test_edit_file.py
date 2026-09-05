from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import code_rook.core.editing.engine as engine_module
import code_rook.core.editing.transaction as transaction_module
from code_rook.core.editing import content_hash
from code_rook.core.tools.builtin.edit_file import EditFileTool
from code_rook.core.tools.builtin.read_file import ReadFileTool
from code_rook.core.tools.builtin.write_file import WriteFileTool


def _payload(content: str) -> dict:
    return json.loads(content)


def _read_hash(content: str) -> str:
    metadata_line = content.splitlines()[0]
    return json.loads(metadata_line.removeprefix("[metadata] "))["content_hash"]


async def test_edit_file_replaces_unique_text_and_returns_diff(tmp_path: Path) -> None:
    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    original = b"def answer():\n    return 41\n"
    target.write_bytes(original)

    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "src/app.py",
        "old_text": "return 41",
        "new_text": "return 42",
        "expected_hash": content_hash(original),
    })
    data = _payload(result.content)

    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
    assert data["path"] == "src/app.py"
    assert data["replacements"] == 1
    assert data["old_hash"] == content_hash(original)
    assert data["new_hash"] == content_hash(target.read_bytes())
    assert "-    return 41" in data["diff"]
    assert "+    return 42" in data["diff"]
    assert data["diff_truncated"] is False
    assert data["additions"] == 1
    assert data["deletions"] == 1


# 功能：验证模型以 LF 提交的多行编辑可以命中 Windows CRLF 文件并保留原行尾
# 设计：直接写入 CRLF 字节后跨两行替换，断言未触发整文件重写且结果仍全部使用 CRLF
async def test_edit_file_adapts_multiline_match_to_existing_crlf(tmp_path: Path) -> None:
    target = tmp_path / "value.py"
    target.write_bytes(b"def value():\r\n    return 41\r\n")

    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "value.py",
        "old_text": "def value():\n    return 41\n",
        "new_text": "def value():\n    return 42\n",
    })

    assert not result.is_error
    assert target.read_bytes() == b"def value():\r\n    return 42\r\n"


async def test_read_hash_can_be_passed_directly_to_edit(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("old value\n", encoding="utf-8")
    read_result = await ReadFileTool(workspace_root=tmp_path).invoke({"path": "note.txt"})

    edit_result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "note.txt",
        "old_text": "old",
        "new_text": "new",
        "expected_hash": _read_hash(read_result.content),
    })

    assert not edit_result.is_error
    assert target.read_text(encoding="utf-8") == "new value\n"


async def test_edit_file_rejects_ambiguous_match_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "values.txt"
    target.write_text("same\nsame\n", encoding="utf-8")

    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "values.txt",
        "old_text": "same",
        "new_text": "changed",
    })

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "ambiguous_match"
    assert target.read_text(encoding="utf-8") == "same\nsame\n"


async def test_edit_file_replace_all_and_delete(tmp_path: Path) -> None:
    target = tmp_path / "values.txt"
    target.write_text("keep x, x\n", encoding="utf-8")

    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "values.txt",
        "old_text": "x",
        "new_text": "",
        "replace_all": True,
    })

    assert not result.is_error
    assert _payload(result.content)["replacements"] == 2
    assert target.read_text(encoding="utf-8") == "keep , \n"


async def test_edit_file_detects_read_hash_mismatch(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    original = b"before\n"
    target.write_bytes(original)
    seen_hash = content_hash(original)
    target.write_text("user changed before\n", encoding="utf-8")

    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "note.txt",
        "old_text": "before",
        "new_text": "after",
        "expected_hash": seen_hash,
    })

    assert result.is_error
    assert result.error_type == "conflict"
    assert _payload(result.content)["error"]["code"] == "hash_mismatch"
    assert target.read_text(encoding="utf-8") == "user changed before\n"


async def test_edit_file_detects_change_during_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    original = b"before\n"
    target.write_bytes(original)
    real_write_temp = engine_module._write_temp_file

    def write_temp_then_change(path: Path, content: bytes, mode: int | None) -> Path:
        temp_path = real_write_temp(path, content, mode)
        path.write_text("user won\n", encoding="utf-8")
        return temp_path

    monkeypatch.setattr(engine_module, "_write_temp_file", write_temp_then_change)
    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "note.txt",
        "old_text": "before",
        "new_text": "after",
        "expected_hash": content_hash(original),
    })

    assert result.is_error
    assert _payload(result.content)["error"]["code"] == "concurrent_change"
    assert target.read_text(encoding="utf-8") == "user won\n"
    assert list(tmp_path.glob(".note.txt.*.tmp")) == []


async def test_atomic_write_failure_preserves_original_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(transaction_module.os, "replace", fail_replace)
    result = await WriteFileTool(workspace_root=tmp_path).invoke({
        "path": "note.txt",
        "content": "replacement",
    })

    assert result.is_error
    assert "replace failed" in result.content
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.glob(".note.txt.*.tmp")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable on Windows")
async def test_edit_file_preserves_existing_mode(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_text("echo old\n", encoding="utf-8")
    target.chmod(0o755)

    result = await EditFileTool(workspace_root=tmp_path).invoke({
        "path": "script.sh",
        "old_text": "old",
        "new_text": "new",
    })

    assert not result.is_error
    assert target.stat().st_mode & 0o777 == 0o755


async def test_edit_file_rejects_workspace_escape(tmp_path: Path) -> None:
    with pytest.raises(PermissionError):
        await EditFileTool(workspace_root=tmp_path).invoke({
            "path": "../outside.txt",
            "old_text": "old",
            "new_text": "new",
        })

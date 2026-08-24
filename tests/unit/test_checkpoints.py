from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.checkpoints import CheckpointError, CheckpointStore
from code_rook.core.config import CodeRookConfig
from code_rook.core.editing import FileMutation, apply_file_transaction
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.builtin.apply_patch import ApplyPatchTool
from code_rook.core.tools.builtin.checkpoint import (
    CheckpointListTool,
    CheckpointRewindTool,
)
from code_rook.core.tools.builtin.edit_file import EditFileTool
from code_rook.core.tools.builtin.write_file import WriteFileTool
from code_rook.core.workspace import WorkspaceBoundary


def _store(tmp_path: Path) -> CheckpointStore:
    return CheckpointStore(tmp_path / ".runtime" / "checkpoints", WorkspaceBoundary(tmp_path))


def _payload(content: str) -> dict:
    return json.loads(content)


def test_checkpoint_persists_and_rewinds_modify_add_delete(tmp_path: Path) -> None:
    modify = tmp_path / "modify.txt"
    delete = tmp_path / "delete.txt"
    added = tmp_path / "nested" / "added.txt"
    modify.write_bytes(b"before\n")
    delete.write_bytes(b"remove me\n")
    mutations = [
        FileMutation(modify, b"before\n", b"after\n"),
        FileMutation(added, None, b"created\n"),
        FileMutation(delete, b"remove me\n", None),
    ]
    store = _store(tmp_path)
    checkpoint_id = store.create(mutations, label="multi-file edit")
    apply_file_transaction(tmp_path, mutations)

    restarted_store = _store(tmp_path)
    outcome = restarted_store.rewind(checkpoint_id)

    assert outcome.restored == ["modify.txt", "nested/added.txt", "delete.txt"]
    assert modify.read_bytes() == b"before\n"
    assert not added.exists()
    assert delete.read_bytes() == b"remove me\n"
    listed = restarted_store.list_checkpoints()
    assert listed[0].checkpoint_id == checkpoint_id
    assert listed[0].status == "rewound"


def test_checkpoint_conflict_aborts_every_restore(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"before first\n")
    second.write_bytes(b"before second\n")
    mutations = [
        FileMutation(first, b"before first\n", b"after first\n"),
        FileMutation(second, b"before second\n", b"after second\n"),
    ]
    store = _store(tmp_path)
    checkpoint_id = store.create(mutations, label="two files")
    apply_file_transaction(tmp_path, mutations)
    second.write_bytes(b"user changed\n")

    with pytest.raises(CheckpointError) as error:
        store.rewind(checkpoint_id)

    assert error.value.code == "rewind_conflict"
    assert error.value.conflicts == ["second.txt"]
    assert first.read_bytes() == b"after first\n"
    assert second.read_bytes() == b"user changed\n"


# 功能：验证 rewind 预览摘要绑定 checkpoint 与当前文件状态
# 设计：预览后修改目标文件，再携带旧摘要执行恢复，断言任何内容都不会被覆盖
def test_rewind_rejects_stale_preview_digest(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before\n")
    mutation = FileMutation(target, b"before\n", b"after\n")
    store = _store(tmp_path)
    checkpoint_id = store.create([mutation], label="preview")
    apply_file_transaction(tmp_path, [mutation])
    preview = store.preview_rewind(checkpoint_id)
    target.write_bytes(b"changed after preview\n")

    with pytest.raises(CheckpointError) as error:
        store.rewind(checkpoint_id, expected_digest=preview.state_digest)

    assert error.value.code == "rewind_preview_stale"
    assert target.read_bytes() == b"changed after preview\n"


# 功能：验证只读打开不存在的 checkpoint 根目录不会产生文件系统副作用
# 设计：使用 create=False 构造存储并立即列表，断言结果为空且根目录仍不存在
def test_checkpoint_read_only_open_does_not_create_directories(tmp_path: Path) -> None:
    root = tmp_path / "missing" / "checkpoints"

    store = CheckpointStore(
        root,
        WorkspaceBoundary(tmp_path),
        create=False,
    )

    assert store.list_checkpoints() == []
    assert not root.exists()


def test_checkpoint_handles_partially_applied_crash_state(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"before first\n")
    second.write_bytes(b"before second\n")
    mutations = [
        FileMutation(first, b"before first\n", b"after first\n"),
        FileMutation(second, b"before second\n", b"after second\n"),
    ]
    store = _store(tmp_path)
    checkpoint_id = store.create(mutations, label="interrupted")
    first.write_bytes(b"after first\n")

    outcome = store.rewind(checkpoint_id)

    assert outcome.restored == ["first.txt"]
    assert outcome.already_restored == ["second.txt"]
    assert first.read_bytes() == b"before first\n"
    assert second.read_bytes() == b"before second\n"


async def test_edit_file_creates_checkpoint_and_rewind_tool_restores(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("old\n", encoding="utf-8")
    store = _store(tmp_path)
    edit_result = await EditFileTool(
        workspace_root=tmp_path,
        checkpoint_store=store,
    ).invoke({"path": "value.txt", "old_text": "old", "new_text": "new"})
    checkpoint_id = _payload(edit_result.content)["checkpoint_id"]

    list_result = await CheckpointListTool(store).invoke({})
    rewind_result = await CheckpointRewindTool(store).invoke({
        "checkpoint_id": checkpoint_id
    })

    assert _payload(list_result.content)["checkpoint_count"] == 1
    assert not rewind_result.is_error
    assert target.read_text(encoding="utf-8") == "old\n"


async def test_write_new_file_checkpoint_rewind_removes_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    target = tmp_path / "created.txt"

    write_result = await WriteFileTool(
        workspace_root=tmp_path,
        checkpoint_store=store,
    ).invoke({"path": "created.txt", "content": "created\n"})
    checkpoint_id = store.list_checkpoints()[0].checkpoint_id
    rewind_result = await CheckpointRewindTool(store).invoke({
        "checkpoint_id": checkpoint_id
    })

    assert not write_result.is_error
    assert _payload(write_result.content)["checkpoint_id"] == checkpoint_id
    assert not rewind_result.is_error
    assert not target.exists()


async def test_apply_patch_checkpoint_restores_all_files(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    first.write_text("old\n", encoding="utf-8")
    store = _store(tmp_path)
    patch = """\
--- a/first.txt
+++ b/first.txt
@@ -1 +1 @@
-old
+new
--- /dev/null
+++ b/added.txt
@@ -0,0 +1 @@
+added
"""

    patch_result = await ApplyPatchTool(
        workspace_root=tmp_path,
        checkpoint_store=store,
    ).invoke({"patch": patch})
    checkpoint_id = _payload(patch_result.content)["checkpoint_id"]
    rewind_result = await CheckpointRewindTool(store).invoke({
        "checkpoint_id": checkpoint_id
    })

    assert not patch_result.is_error
    assert not rewind_result.is_error
    assert first.read_text(encoding="utf-8") == "old\n"
    assert not (tmp_path / "added.txt").exists()


async def test_failed_edit_discards_checkpoint(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_text("current\n", encoding="utf-8")
    store = _store(tmp_path)

    result = await EditFileTool(
        workspace_root=tmp_path,
        checkpoint_store=store,
    ).invoke({"path": "value.txt", "old_text": "missing", "new_text": "new"})

    assert result.is_error
    assert store.list_checkpoints() == []


def test_checkpoint_rejects_invalid_id_and_tampered_blob(tmp_path: Path) -> None:
    target = tmp_path / "value.txt"
    target.write_bytes(b"before\n")
    store = _store(tmp_path)
    mutation = FileMutation(target, b"before\n", b"after\n")
    checkpoint_id = store.create([mutation], label="tamper test")
    apply_file_transaction(tmp_path, [mutation])

    with pytest.raises(CheckpointError, match="invalid checkpoint id"):
        store.rewind("../manifest")

    blob = next((tmp_path / ".runtime" / "checkpoints" / "blobs").iterdir())
    blob.write_bytes(b"corrupt")
    with pytest.raises(CheckpointError) as error:
        store.rewind(checkpoint_id)
    assert error.value.code == "blob_corrupt"


def test_runner_registry_exposes_checkpoint_tools_when_store_is_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = AgentRunner(CodeRookConfig(), runs_dir=tmp_path / ".runs")
    store = _store(tmp_path)

    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        checkpoint_store=store,
    )

    assert registry.get("checkpoint_list") is not None
    assert registry.get("checkpoint_rewind") is not None

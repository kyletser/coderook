from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from code_rook.core.app import CoreApp
from code_rook.core.artifacts import (
    ArtifactCorruptError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactStore,
    inspect_image,
)
from code_rook.core.artifacts.store import scan_referenced_artifact_shas
from code_rook.core.tools.artifact import ArtifactReadTool


# 功能：验证 artifact 按内容寻址去重，并支持有界连续切片读取
# 设计：重复写入同一 UTF-8 内容比较引用，再用 next_offset 拼接完整正文
async def test_artifact_store_deduplicates_and_reads_ranges(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = await store.put("abcdefghij")
    second = await store.put("abcdefghij")

    head = await store.read(first.sha256, offset=0, limit=4)
    tail = await store.read(first.sha256, offset=head.next_offset or 0, limit=20)

    assert first == second
    assert first.handle == f"artifact:{first.sha256}"
    assert first.size == 10
    assert first.path.endswith(first.sha256)
    assert head.content == "abcd"
    assert head.next_offset == 4
    assert tail.content == "efghij"
    assert tail.next_offset is None


# 功能：验证 artifact 丢失与 hash 不匹配分别返回明确错误类别
# 设计：先读不存在的合法 hash，再篡改真实内容文件，排除路径错误与内容损坏混淆
async def test_artifact_store_reports_missing_and_corrupt(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    with pytest.raises(ArtifactNotFoundError):
        await store.read("0" * 64)

    reference = await store.put("trusted")
    (root / reference.sha256).write_text("tampered", encoding="utf-8")

    with pytest.raises(ArtifactCorruptError):
        await store.read(reference.sha256)


# 功能：验证图片 artifact 可按完整 hash 有界读取并从内容头取得尺寸与 MIME
# 设计：构造最小 PNG 头而不依赖图像库，固定 extensionless artifact 的发送前校验契约
async def test_image_artifact_is_content_detected_and_bounded(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (3).to_bytes(4, "big") + (
        5
    ).to_bytes(4, "big")
    store = ArtifactStore(tmp_path / "artifacts")
    reference = await store.put(png, media_type="image/png")

    restored = await store.read_bytes(reference.sha256, max_bytes=1024)
    metadata = inspect_image(restored)

    assert restored == png
    assert metadata.media_type == "image/png"
    assert (metadata.width, metadata.height) == (3, 5)
    with pytest.raises(ArtifactError, match="exceeds"):
        await store.read_bytes(reference.sha256, max_bytes=4)


# 功能：验证 artifact_read 工具只返回请求范围并保留 corrupt 结构化错误码
# 设计：先读取中间切片，再篡改文件并从 ToolResult JSON 断言 error.code
async def test_artifact_read_tool_returns_bounded_structured_result(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(root)
    reference = await store.put("0123456789")
    tool = ArtifactReadTool(store)

    result = await tool.invoke(
        {"handle": reference.handle, "offset": 3, "limit": 4}
    )
    payload = json.loads(result.content)
    assert payload["content"] == "3456"
    assert payload["next_offset"] == 7

    missing = await tool.invoke({"handle": f"artifact:{'0' * 64}"})
    assert missing.is_error
    assert json.loads(missing.content)["error"]["code"] == "artifact_unavailable"

    (root / reference.sha256).write_text("broken", encoding="utf-8")
    corrupt = await tool.invoke({"handle": reference.handle})
    assert corrupt.is_error
    assert json.loads(corrupt.content)["error"]["code"] == "artifact_corrupt"


# ── GC（W3.4 #17） ────────────────────────────────────────────────────────────

# 功能：list_gc_candidates 只列出超过保留龄且未被 keep 引用的 artifact
# 设计：固定 now 使 mtime 判定可复现；写两份 artifact 后调整 mtime 分新旧，再传 keep 保引用
async def test_gc_candidates_respect_age_and_keep(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    old = (await store.put("old content")).sha256
    fresh = (await store.put("fresh content")).sha256
    old_path, fresh_path = (tmp_path / "artifacts" / s for s in (old, fresh))
    now = 2_000_000_000.0
    os.utime(old_path, (now - 40 * 86400, now - 40 * 86400))
    os.utime(fresh_path, (now - 1 * 86400, now - 1 * 86400))

    candidates = store.list_gc_candidates(days=30, now=now)
    assert [p.name for p in candidates] == [old]
    kept = store.list_gc_candidates(days=30, now=now, keep={old})
    assert kept == []


# 功能：dry_run 不清除，非 dry_run 只删除候选并返回清单
# 设计：先 dry_run 断言文件仍在，再以同样 now 执行删除断言目录中只剩 keep 引用项
async def test_gc_dry_run_then_delete(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    a = (await store.put("a")).sha256
    b = (await store.put("b")).sha256
    paths = {tmp_path / "artifacts" / s for s in (a, b)}
    now = 2_000_000_000.0
    for p in paths:
        os.utime(p, (now - 40 * 86400, now - 40 * 86400))

    assert store.gc(days=30, now=now, dry_run=True) == sorted(paths, key=lambda p: p.name)
    assert all(p.exists() for p in paths)
    removed = store.gc(days=30, now=now, dry_run=False)
    assert {p.name for p in removed} == {a, b}
    assert not store._root.exists() or list(store._root.iterdir()) == []


# 功能：scan_referenced_artifact_shas 从文本中提取全部 artifact 引用作为 keep 集
# 设计：写含两个引用的文件，断言返回集合精确匹配，供 GC 前计算保留项
def test_scan_referenced_artifact_shas_extracts_refs(tmp_path: Path) -> None:
    file = tmp_path / "session.jsonl"
    sha = "ab" * 32
    other = "cd" * 32
    file.write_text(
        f'{{"handle":"artifact:{sha}"}}\n{{"ref":"artifact:{other}"}}\n',
        encoding="utf-8",
    )
    assert scan_referenced_artifact_shas([file]) == {sha, other}


# 功能：验证 inventory 同时报告大小、引用状态、年龄和 GC 候选判定
# 设计：固定 mtime/now 并保留其中一份旧 artifact，区分 referenced 与 candidate 两条状态
async def test_artifact_inventory_explains_gc_decision(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    kept = (await store.put("keep")).sha256
    candidate = (await store.put("remove")).sha256
    now = 2_000_000_000.0
    for sha in (kept, candidate):
        os.utime(
            tmp_path / "artifacts" / sha,
            (now - 40 * 86400, now - 40 * 86400),
        )

    inventory = store.inventory(days=30, keep={kept}, now=now)
    by_sha = {item.sha256: item for item in inventory}

    assert by_sha[kept].referenced is True
    assert by_sha[kept].gc_candidate is False
    assert by_sha[candidate].referenced is False
    assert by_sha[candidate].gc_candidate is True
    assert by_sha[candidate].size == len("remove")


# 功能：验证 daemon GC 默认 dry-run，确认后重新扫描并删除且写入 receipt
# 设计：在临时 HOME/工作区构造过期 artifact，连续调用同一类型化 handler 覆盖预览与确认
async def test_artifact_gc_handler_requires_confirmation_and_writes_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.chdir(tmp_path)
    app = CoreApp()
    reference = await app._artifact_store.put("expired")
    artifact_path = tmp_path / ".coderook" / "artifacts" / reference.sha256
    old = time.time() - 40 * 86400
    os.utime(artifact_path, (old, old))

    preview = await app._artifact_gc_handler({"days": 30})
    assert preview.dry_run is True
    assert artifact_path.exists()

    applied = await app._artifact_gc_handler({"days": 30, "confirmed": True})
    assert applied.dry_run is False
    assert applied.removed == [reference.sha256]
    assert not artifact_path.exists()
    assert Path(applied.receipt_path).is_file()

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.artifacts import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactStore,
)
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

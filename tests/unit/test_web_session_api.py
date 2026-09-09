from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from code_rook.core.api.service import RuntimeApiService
from code_rook.core.workspace import WorkspaceBoundary


# 功能：工作区文件搜索优先返回文件名前缀匹配，而不是较早遍历到的路径子串
# 设计：在临时工作区混放 README 与 weread 文件，并限制结果数以覆盖排序发生在截断之前
async def test_workspace_file_search_ranks_name_prefix_before_path_substring(
    tmp_path: Path,
) -> None:
    (tmp_path / "weread_refresh.py").write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text("root", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README-guide.md").write_text("guide", encoding="utf-8")
    service = RuntimeApiService(
        MagicMock(),
        MagicMock(),
        workspace_boundary=WorkspaceBoundary(tmp_path),
    )

    result = await service.list_workspace_files(query="READ", limit=2)

    entries = result["entries"]
    assert isinstance(entries, list)
    assert [entry["path"] for entry in entries] == [
        "README.md",
        "docs/README-guide.md",
    ]
    assert result["truncated"] is True


# 功能：Web 从用户消息新建分支时返回原文草稿，并把新会话定位到该消息之前
# 设计：用最小异步 facade 替身验证 fork 后必经正式树导航，避免复制两套分支解析逻辑
async def test_web_fork_returns_editable_user_message() -> None:
    sessions = MagicMock()
    sessions.fork = AsyncMock(return_value=SimpleNamespace(id="forked"))
    sessions.navigate_tree = AsyncMock(return_value={"editor_text": "修改这个问题"})
    thread = MagicMock()
    thread.model_dump.return_value = {"id": "forked", "title": "Task (fork)"}
    runtime = MagicMock()
    runtime.get_thread = AsyncMock(return_value=thread)
    service = RuntimeApiService(runtime, sessions)

    result = await service.fork_thread("source", leaf_seq=42)

    sessions.fork.assert_awaited_once_with("source", "", leaf_seq=42)
    sessions.navigate_tree.assert_awaited_once_with("forked", 42)
    assert result["editor_text"] == "修改这个问题"
    assert result["id"] == "forked"

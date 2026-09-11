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


# 功能：Web 从用户消息新建分支时返回原文草稿，并只写入一次已解析的分支选择
# 设计：用最小异步 facade 替身先解析源节点再 Fork，防止在新会话重复导航和追加事件
async def test_web_fork_returns_editable_user_message() -> None:
    sessions = MagicMock()
    sessions.fork = AsyncMock(return_value=SimpleNamespace(id="forked"))
    sessions.navigation_target = AsyncMock(return_value={
        "leaf_seq": 36, "editor_text": "修改这个问题",
    })
    thread = MagicMock()
    thread.model_dump.return_value = {"id": "forked", "title": "Task (fork)"}
    runtime = MagicMock()
    runtime.get_thread = AsyncMock(return_value=thread)
    service = RuntimeApiService(runtime, sessions)

    result = await service.fork_thread("source", leaf_seq=42)

    sessions.navigation_target.assert_awaited_once_with("source", 42)
    sessions.fork.assert_awaited_once_with("source", "", leaf_seq=36)
    assert result["editor_text"] == "修改这个问题"
    assert result["id"] == "forked"


# 功能：Web 提交前可以刷新过期 Provider 收据并拿到最新 readiness
# 设计：注入最小配置服务，断言刷新只调用一次统一探测且响应来自刷新后的目录快照
async def test_web_refreshes_provider_readiness_before_turn() -> None:
    configuration = MagicMock()
    configuration.probe_readiness = AsyncMock()
    readiness = MagicMock()
    readiness.model_dump.return_value = {
        "status": "provider_verified",
        "local_ready": True,
        "reason": "verified",
    }
    configuration.snapshot.return_value = SimpleNamespace(
        active_route_id="aliyun",
        routes=(),
        credential_sources={},
        readiness=readiness,
        route_issues=(),
    )
    service = RuntimeApiService(
        MagicMock(),
        MagicMock(),
        configuration=configuration,
    )

    result = await service.refresh_provider_readiness()

    configuration.probe_readiness.assert_awaited_once_with()
    assert result["active_route_id"] == "aliyun"
    assert result["readiness"] == {
        "status": "provider_verified",
        "local_ready": True,
        "reason": "verified",
    }

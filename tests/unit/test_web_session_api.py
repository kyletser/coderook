from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from code_rook.core.api.service import RuntimeApiService


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

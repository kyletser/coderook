import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from code_rook.core.agent_runtime.shell import CodingShellTool, bash_executable
from code_rook.core.agent_runtime.user_shell import execute_user_shell, parse_user_shell
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.runner import AgentRunner
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.store import SessionStore
from code_rook.core.tools.registry import ToolRegistry


# 功能：! 保存执行结果到上下文，!! 排除上下文，两者保持原命令引号和变量。
# 设计：不运行命令，只比较解析值，证明不会经过模板替换或 Shell 分词再拼接。
def test_user_shell_prefixes() -> None:
    command = 'printf "%s" "$VALUE"'
    included = parse_user_shell("!" + command)
    excluded = parse_user_shell("!!" + command)
    assert included is not None and excluded is not None
    assert included.command == excluded.command == command
    assert included.include_in_context and not excluded.include_in_context
    assert parse_user_shell("hello") is None
    assert parse_user_shell("!!  ") is None


# 功能：用户命令通过正式工具管线直接执行并返回真实非零状态，无 Provider 参与。
# 设计：构造真实 Bash 工具和事件总线，命令可观测地输出文本并退出 7。
async def test_user_shell_executes_without_provider(tmp_path: Path) -> None:
    try:
        bash_executable()
    except RuntimeError:
        pytest.skip("Bash unavailable")
    registry = ToolRegistry()
    registry.register(CodingShellTool(tmp_path, None, None))
    request = parse_user_shell("!printf direct-output; exit 7")
    assert request is not None
    result = await execute_user_shell(
        request, registry=registry, bus=EventBus(), run_id="user-shell", operation_id="shell-1",
    )
    assert "direct-output" in result.content
    assert result.is_error
    assert result.process_usage == {"exit_code": 7}


# 功能：直接命令经会话入口执行并返回结果，!! 不进入模型历史且不解析路由。
# 设计：使用真实 Runner、账本与 Bash，路由替身若被调用立即失败，覆盖完整派发链。
@pytest.mark.parametrize("prefix", ["!", "!!"])
async def test_session_shell_history(tmp_path: Path, prefix: str) -> None:
    bus = EventBus()
    store = SessionStore(tmp_path / "sessions")
    routes = Mock()
    routes.resolve.side_effect = AssertionError("shell must not resolve model")
    runner = AgentRunner(CodeRookConfig(), bus=bus, workspace_root=tmp_path)
    manager = SessionManager(
        store, lambda: runner, bus, workspace=tmp_path, route_registry=routes,
    )
    session = await manager.create("chat")
    try:
        run_id = await manager.send_message(session.id, prefix + "printf shell-result")
        async with asyncio.timeout(15):
            active = manager._active_runs.get(run_id)
            if active is not None:
                await active.finished.wait()
        history = str(store.read_messages(session.id))
        assert ("shell-result" in history) == (prefix == "!")
        reopened = SessionStore(tmp_path / "sessions")
        display = reopened.derive_messages(session.id, display=True)
        shell = next(item for item in display if item["role"] == "bashExecution")
        assert shell["command"] == "printf shell-result"
        assert shell["output"] == "shell-result"
        assert shell["exclude_from_context"] == (prefix == "!!")
        assert reopened.read_messages(session.id) == store.read_messages(session.id)
        assert await manager.get_display_history(session.id) == display
        events = (store.runs_dir(session.id) / run_id / "events.jsonl").read_text("utf-8")
        assert '"shell-result"' in events
        assert '"status":"success"' in events.replace(" ", "")
        routes.resolve.assert_not_called()
    finally:
        await manager.cancel_all()

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from code_rook.core.app import CoreApp
from code_rook.core.authority import (
    AuthorityProfile,
    RuntimeMode,
    ToolAction,
    WorkspaceTrust,
)
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.session.model import Session


async def test_agent_run_handler_scopes_and_cleans_headless_mode() -> None:
    manager = PermissionManager()
    checked = asyncio.Event()
    decisions: list[tuple[bool, str]] = []
    session = Session("sess-headless", "one_shot", "active", "", "t", "t")

    class _Sessions:
        async def create(self, mode: str, title: str = "") -> Session:
            return session

        async def send_message(
            self,
            session_id: str,
            content: str,
            *,
            run_id: str | None = None,
        ) -> str:
            async def emit(_event: dict[str, Any]) -> None:
                raise AssertionError("headless permission mode must not request input")

            decisions.append(
                await manager.check_and_wait(
                    tool_use_id="edit-1",
                    tool_name="edit_file",
                    params={"path": "x", "old_text": "a", "new_text": "b"},
                    session_id=session_id,
                    event_emitter=emit,
                )
            )
            checked.set()
            return run_id or ""

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    result = await app._agent_run_handler({  # type: ignore[attr-defined]
        "goal": "edit",
        "permission_mode": "allow_list",
        "allow_tools": ["edit_file"],
    })
    await asyncio.wait_for(checked.wait(), timeout=1)
    await asyncio.sleep(0)

    assert result.run_id
    assert decisions == [(True, "headless_allow_list")]
    assert session.id not in manager._session_modes  # type: ignore[attr-defined]
    assert app._running_runs == set()  # type: ignore[attr-defined]


# 功能：验证会话 authority 更新只替换 mode/profile，并可由查询命令原样读回
# 设计：预置收窄的 action scope 后直接调用 Core handler，确保权限切换不会隐式扩大能力
async def test_session_authority_handlers_preserve_scope() -> None:
    manager = PermissionManager()
    session = Session("sess-authority", "chat", "active", "", "t", "t")
    original = manager.get_authority_snapshot(session.id).model_copy(
        update={"allowed_actions": frozenset({ToolAction.READ, ToolAction.MUTATE})}
    )
    manager.set_authority_snapshot(session.id, original)

    class _Sessions:
        # 返回测试会话并固定存在性校验
        def get_session(self, session_id: str) -> Session:
            assert session_id == session.id
            return session

        # 模拟当前没有运行中的 turn
        def is_busy(self, session_id: str) -> bool:
            assert session_id == session.id
            return False

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    updated = await app._session_set_authority_handler(  # type: ignore[attr-defined]
        {
            "session_id": session.id,
            "mode": "plan",
            "profile": "auto_review",
        }
    )
    restored = await app._session_get_authority_handler(  # type: ignore[attr-defined]
        {"session_id": session.id}
    )

    assert updated.snapshot.mode == RuntimeMode.PLAN
    assert updated.snapshot.profile == AuthorityProfile.AUTO_REVIEW
    assert updated.snapshot.allowed_actions == original.allowed_actions
    assert restored.snapshot == updated.snapshot
    assert (
        manager.get_authority_snapshot("new-session").profile
        == AuthorityProfile.AUTO_REVIEW
    )

    trust_only = await app._session_set_authority_handler(  # type: ignore[attr-defined]
        {"session_id": session.id, "workspace_trust": "trusted"}
    )
    assert trust_only.snapshot.workspace_trust == WorkspaceTrust.TRUSTED
    assert trust_only.snapshot.mode == RuntimeMode.PLAN
    assert trust_only.snapshot.profile == AuthorityProfile.AUTO_REVIEW


# 功能：验证运行中的 turn 不能通过协议静默改变 authority 快照
# 设计：让会话服务明确报告 busy，断言 handler 在写入 PermissionManager 前返回结构化错误
async def test_session_authority_change_rejected_while_turn_is_busy() -> None:
    manager = PermissionManager()
    session = Session("sess-busy", "chat", "active", "", "t", "t")

    class _Sessions:
        # 返回存在的测试会话
        def get_session(self, session_id: str) -> Session:
            assert session_id == session.id
            return session

        # 模拟 turn 正持有执行锁
        def is_busy(self, session_id: str) -> bool:
            assert session_id == session.id
            return True

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    with pytest.raises(HandlerError, match="active turn"):
        await app._session_set_authority_handler(  # type: ignore[attr-defined]
            {"session_id": session.id, "mode": "plan"}
        )
    assert manager.get_authority_snapshot(session.id).mode == RuntimeMode.ACT

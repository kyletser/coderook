from __future__ import annotations

import asyncio
from typing import Any

from code_rook.core.app import CoreApp
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

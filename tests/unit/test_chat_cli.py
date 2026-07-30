from __future__ import annotations

from typing import Any

import pytest

from code_rook.cli.commands.chat import ChatPrinter, _cancel_active_run


async def test_chat_printer_tracks_and_clears_active_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = ChatPrinter()

    await printer.handle({"type": "run.started", "run_id": "run-1"})
    assert printer.active_run_id == "run-1"

    await printer.handle({
        "type": "session.interrupted",
        "session_id": "sess-1",
        "last_run_id": "run-1",
    })
    assert printer.active_run_id is None
    assert "run cancelled" in capsys.readouterr().out


async def test_chat_cancel_sends_run_cancel() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeClient:
        async def send_command(
            self,
            method: str,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            calls.append((method, params))
            return {"status": "cancelled"}

    printer = ChatPrinter()
    printer.active_run_id = "run-1"

    cancelled = await _cancel_active_run(_FakeClient(), printer)  # type: ignore[arg-type]

    assert cancelled
    assert calls == [("run.cancel", {"run_id": "run-1"})]

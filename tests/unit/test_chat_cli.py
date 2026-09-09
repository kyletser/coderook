from __future__ import annotations

from typing import Any

import pytest

from code_rook.cli.commands.chat import ChatPrinter, _cancel_active_run


# 功能：验证非流式 Provider 的最终 agent.message 会直接显示在交互式 chat 中
# 设计：发送不含 llm.token 的完整消息事件，断言正文只打印一次且不是只剩等待提示
async def test_chat_printer_shows_non_streaming_final_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = ChatPrinter()
    await printer.handle({"type": "run.started", "run_id": "run-answer"})
    await printer.handle(
        {
            "type": "agent.message",
            "run_id": "run-answer",
            "phase": "end",
            "role": "assistant",
            "content": [{"type": "text", "text": "完整回答"}],
        }
    )

    assert capsys.readouterr().out == "完整回答\n"


# 功能：验证流式回答不会在 agent.message 结束事件到达后重复整段输出
# 设计：先送 token 再送同正文的终态事件，断言输出只含一份文本并正确补换行
async def test_chat_printer_deduplicates_streamed_final_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = ChatPrinter()
    await printer.handle({"type": "run.started", "run_id": "run-stream"})
    await printer.handle({"type": "llm.token", "run_id": "run-stream", "token": "流式回答"})
    await printer.handle(
        {
            "type": "agent.message",
            "run_id": "run-stream",
            "phase": "end",
            "role": "assistant",
            "content": [{"type": "text", "text": "流式回答"}],
        }
    )

    assert capsys.readouterr().out == "流式回答\n"


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

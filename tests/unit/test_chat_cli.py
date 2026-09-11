from __future__ import annotations

import asyncio
from typing import Any

import pytest

from code_rook.cli.commands.chat import (
    ChatPrinter,
    _cancel_active_run,
    _cancel_interrupted_run,
    _chat_async,
    _is_exit_command,
    _unwrap_runtime_event,
)
from code_rook.core.config import CodeRookConfig


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


# 功能：Chat 实时通道只显示当前会话和当前 Turn 的非持久事件。
# 设计：混入其他会话生命周期与其他 Run token，断言仅目标事件改变状态并产生输出。
async def test_chat_printer_filters_global_live_events(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = ChatPrinter()
    printer.session_id = "sess-current"
    await printer.handle({"type": "run.started", "run_id": "run-current"})

    await printer.handle(
        {
            "type": "session.interrupted",
            "session_id": "sess-other",
            "last_run_id": "run-other",
        }
    )
    await printer.handle({"type": "llm.token", "run_id": "run-other", "token": "错误"})
    await printer.handle({"type": "llm.token", "run_id": "run-current", "token": "正确"})

    assert printer.active_run_id == "run-current"
    assert capsys.readouterr().out == "正确"


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
    assert printer.active_run_id is None


# 功能：主 Chat 事件循环被 Ctrl+C 打断后可用新连接取消 Core 中仍活动的 Run。
# 设计：模拟完整连接、读循环和取消响应，断言独立连接被关闭且只取消目标 Run。
async def test_chat_cancels_interrupted_run_with_fresh_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeClient:
        # 模拟成功建立补偿取消连接。
        async def connect(self) -> None:
            return None

        # 记录精确 Run 取消请求。
        async def send_command(
            self,
            method: str,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            calls.append((method, params))
            return {"status": "cancelled"}

        # 保持响应读取循环，直到补偿逻辑完成并取消它。
        async def run_event_loop(self) -> None:
            await asyncio.Event().wait()

        # 标记连接已完成有序关闭。
        async def close(self) -> None:
            calls.append(("close", {}))

    monkeypatch.setattr(
        "code_rook.cli.commands.chat.SocketClient.from_config",
        lambda _config: _FakeClient(),
    )

    assert await _cancel_interrupted_run(CodeRookConfig(), "run-interrupted")
    assert calls == [
        ("run.cancel", {"run_id": "run-interrupted"}),
        ("close", {}),
    ]


# 功能：常见退出词由文本 REPL 本地消费，不进入模型上下文。
# 设计：覆盖带斜杠、普通形式与相似正文，锁定控制指令边界。
def test_chat_exit_commands_are_local() -> None:
    assert all(_is_exit_command(value) for value in ("exit", "QUIT", "/exit", "/quit"))
    assert not _is_exit_command("exiting the function")


# 功能：持久线程订阅产生的 runtime.event 能恢复为 ChatPrinter 可处理的原始事件。
# 设计：使用真实 envelope 字段验证事件类型、会话和 Turn 标识都被正确投影。
def test_chat_unwraps_runtime_event() -> None:
    assert _unwrap_runtime_event(
        {
            "type": "runtime.event",
            "thread_id": "sess-1",
            "turn_id": "turn-1",
            "seq": 7,
            "event_type": "tool.call_started",
            "payload": {"tool_name": "bash"},
        }
    ) == {
        "type": "tool.call_started",
        "tool_name": "bash",
        "session_id": "sess-1",
        "run_id": "turn-1",
    }


# 功能：文本 Chat 异步启动 Turn，并在立即退出时取消活动任务而非等待整个模型循环。
# 设计：用最小 SocketClient 和两行输入驱动真实 REPL 循环，检查 start/cancel 命令顺序。
async def test_chat_starts_turn_without_blocking_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    inputs = iter(["hello", "quit"])

    class _FakeClient:
        # 保存事件处理器，接口形状与真实 SocketClient 一致。
        def on_event(self, handler: object) -> None:
            self.handler = handler

        # 模拟已连接的本地 Core。
        async def connect(self) -> None:
            return None

        # 记录 REPL 命令并为会话与 Turn 返回稳定标识。
        async def send_command(
            self,
            method: str,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            calls.append((method, params))
            if method == "session.create":
                return {"session_id": "sess-1"}
            if method == "turn.start":
                return {"turn_id": "run-1"}
            return {}

        # 保持事件循环存活，直到被测试中的 Chat 清理逻辑取消。
        async def run_event_loop(self) -> None:
            await asyncio.Event().wait()

        # 模拟关闭连接，无需外部资源。
        async def close(self) -> None:
            return None

    client = _FakeClient()
    monkeypatch.setattr(
        "code_rook.cli.commands.chat.SocketClient.from_config",
        lambda _config: client,
    )
    monkeypatch.setattr(
        "code_rook.cli.commands.chat._readline",
        lambda _prompt: asyncio.sleep(0, result=next(inputs)),
    )

    assert await _chat_async(CodeRookConfig()) == 0
    assert ("turn.start", {"thread_id": "sess-1", "content": "hello"}) in calls
    assert ("run.cancel", {"run_id": "run-1"}) in calls
    assert not any(method == "session.send_message" for method, _params in calls)


# 功能：新建文本 Chat 未提交消息就退出时删除空会话。
# 设计：让首个输入直接为退出词，断言没有启动 Turn 且调用 Session 删除。
async def test_chat_discards_new_empty_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeClient:
        # 接受事件回调但不主动产生事件。
        def on_event(self, _handler: object) -> None:
            return None

        # 模拟已连接状态。
        async def connect(self) -> None:
            return None

        # 记录命令并返回新会话标识。
        async def send_command(
            self,
            method: str,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            calls.append((method, params))
            return {"session_id": "sess-empty"} if method == "session.create" else {}

        # 保持事件循环存活，供 finally 验证正确取消。
        async def run_event_loop(self) -> None:
            await asyncio.Event().wait()

        # 模拟无副作用关闭。
        async def close(self) -> None:
            return None

    client = _FakeClient()
    monkeypatch.setattr(
        "code_rook.cli.commands.chat.SocketClient.from_config",
        lambda _config: client,
    )
    monkeypatch.setattr(
        "code_rook.cli.commands.chat._readline",
        lambda _prompt: asyncio.sleep(0, result="exit"),
    )

    assert await _chat_async(CodeRookConfig()) == 0
    assert ("session.delete", {"session_id": "sess-empty"}) in calls
    assert not any(method == "turn.start" for method, _params in calls)

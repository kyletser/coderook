import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.agent_runtime.extensions import ExtensionHost
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.types import LlmResponse
from code_rook.core.runner import AgentRunner
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.manager import SessionManager, _ActiveRun
from code_rook.core.session.store import SessionStore


# 功能：输入改写链保留图片与来源，接管后不再调用后续处理器。
# 设计：同步改写与异步接管串联，检查真实传递对象及末端处理器未触发。
async def test_input_transform_then_handled(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "input")
    images = [{"type": "image", "data": "original", "mimeType": "image/png"}]
    seen = []

    # 第一层只改写文本，不返回图片表示沿用原图片。
    def transform(event: dict) -> dict:
        assert event["source"] == "rpc"
        assert event["streaming_behavior"] == "steer"
        return {"action": "transform", "text": "expanded " + event["text"]}

    # 第二层接收改写后的输入并声明已处理。
    async def handle(event: dict) -> dict:
        seen.append(event)
        return {"action": "handled"}

    host.api.on("input", transform)
    host.api.on("input", handle)
    host.api.on("input", lambda event: pytest.fail("handled must stop the chain"))
    result = await host.emit_input("hello", images, source="rpc", streaming_behavior="steer")
    assert result == {"action": "handled"}
    assert seen[0]["text"] == "expanded hello"
    assert seen[0]["images"] == images
    assert seen[0]["images"] is not images
    await host.close()


# 功能：会话纠偏入口执行输入改写一次，接管的消息不会进入模型待处理队列。
# 设计：使用真实 InteractionManager 检查待处理内容，以未完成任务模拟活动生命周期。
async def test_session_steer_uses_input_hooks(tmp_path: Path) -> None:
    interaction = InteractionManager(EventBus())
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"), MagicMock(), EventBus(),
        interaction_manager=interaction,
    )
    session = await manager.create("chat")
    host = ExtensionHost([], tmp_path, "active")
    manager._extension_hosts[session.id] = host
    task = asyncio.create_task(asyncio.Event().wait())
    manager._active_runs["active"] = _ActiveRun(session.id, task, asyncio.Event())
    interaction.register_run("active")
    calls = []

    # 接管特定输入，其他输入返回改写文本。
    def receive(event: dict) -> dict:
        calls.append(event)
        return ({"action": "handled"} if event["text"] == "local" else
                {"action": "transform", "text": "changed " + event["text"]})

    host.api.on("input", receive)
    try:
        assert await manager.steer_run("active", "hello") == session.id
        assert interaction.drain_steering("active") == ["changed hello"]
        assert await manager.steer_run("active", "local") == session.id
        assert interaction.drain_steering("active") == []
        assert len(calls) == 2
        assert all(event["streaming_behavior"] == "steer" for event in calls)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await host.close()


# 功能：错误处理器不阻断后续改写，显式空图片列表可以移除附件。
# 设计：先抛普通异常再替换输入，单独检查取消异常不会被吞掉。
async def test_input_errors_continue_but_cancellation_propagates(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "input")

    # 模拟扩展实现错误，不将其转成用户输入内容。
    def broken(event: dict) -> None:
        raise ValueError("bad extension")

    host.api.on("input", broken)
    host.api.on("input", lambda event: {"action": "transform", "text": "fixed", "images": []})
    assert await host.emit_input("original", [{"type": "image"}]) == {
        "action": "transform", "text": "fixed", "images": [],
    }

    # 取消是生命周期信号，必须向调用者传播。
    async def cancelled(event: dict) -> None:
        raise asyncio.CancelledError

    host.api.on("input", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await host.emit_input("original")
    await host.close()


# 功能：首次发送和队列派发只改写一次，被接管输入不创建运行或排队记录。
# 设计：真实 Runner 和 SQLite 执行两轮假模型任务，检查请求、历史及队列消费。
async def test_input_admission_and_queue_dispatch(tmp_path: Path) -> None:
    bus = EventBus()
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Answer"))
    runner = AgentRunner(CodeRookConfig(), bus=bus, provider=provider, workspace_root=tmp_path)
    store = SessionStore(tmp_path / "sessions")
    runtime = RuntimeService(RuntimeStore(tmp_path / "runtime.db"), tmp_path, bus=bus)
    manager = SessionManager(store, lambda: runner, bus, runtime_service=runtime, workspace=tmp_path)
    session = await manager.create("chat")
    host = await manager.prepare_extensions(session.id)
    assert host is not None
    calls = []

    # 记录交付次数并明确区分本地接管与正常改写。
    def receive(event: dict) -> dict:
        calls.append(event["text"])
        return ({"action": "handled"} if event["text"] == "local" else
                {"action": "transform", "text": "changed " + event["text"]})

    host.api.on("input", receive)
    try:
        assert await manager.send_message(session.id, "local") == ""
        assert await manager.queue_message(session.id, "local") is None
        assert session.run_ids == []
        assert await manager.list_queued_messages(session.id) == []
        assert provider.chat.await_count == 0
        await manager.send_message(session.id, "first")
        record = await manager.queue_message(session.id, "next")
        assert record is not None and record.content == "changed next"
        task = manager._queue_dispatch_tasks.get(session.id)
        if task is not None:
            await asyncio.wait_for(task, 15)
        assert provider.chat.await_count == 2
        assert calls == ["local", "local", "first", "next"]
        assert "changed first" in str(store.read_messages(session.id))
        assert "changed next" in str(store.read_messages(session.id))
        assert "changed changed" not in str(store.read_messages(session.id))
    finally:
        await manager.cancel_all()

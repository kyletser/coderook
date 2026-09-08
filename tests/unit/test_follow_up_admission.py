import asyncio
import base64
import io
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from code_rook.core.agent_runtime import driver
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import FollowUpMessage, InteractionManager
from code_rook.core.llm.types import LlmResponse
from code_rook.core.loop import AgentLoop
from code_rook.core.runner import AgentRunner
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.store import SessionStore
from code_rook.core.tools.registry import ToolRegistry


# 功能：持久后续队列按配置逐条或整批进入同一个原生循环
# 设计：首次模型请求用屏障挂起，排入两条消息后检查后续请求内容与调用次数
@pytest.mark.parametrize("mode, count", [("one-at-a-time", 3), ("all", 2)])
async def test_session_follow_up_delivery_mode(tmp_path: Path, mode: str, count: int) -> None:
    bus = EventBus()
    interaction = InteractionManager(bus)
    started, release = asyncio.Event(), asyncio.Event()
    requests = []

    # 捕获完整请求；第一轮等待消息入队，其余轮次立即返回。
    async def chat(messages, *_args, **_kwargs):
        requests.append(deepcopy(messages))
        started.set()
        await release.wait()
        return LlmResponse(stop_reason="end_turn", text="Answer")

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    runner = AgentRunner(
        CodeRookConfig(), bus=bus, provider=provider, workspace_root=tmp_path,
        interaction_manager=interaction,
    )
    runtime = RuntimeService(RuntimeStore(tmp_path / "runtime.db"), tmp_path, bus=bus)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: runner, bus,
        runtime_service=runtime, interaction_manager=interaction,
        workspace=tmp_path, follow_up_mode=mode,
    )
    session = await manager.create("chat")
    sending = asyncio.create_task(manager.send_message(session.id, "Initial"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        await manager.queue_message(session.id, "First follow-up")
        await manager.queue_message(session.id, "Second follow-up")
        release.set()
        await asyncio.wait_for(sending, 15)
        assert len(requests) == count
        assert "First follow-up" in str(requests[1])
        assert ("Second follow-up" in str(requests[1])) == (mode == "all")
        assert "Second follow-up" in str(requests[-1])
        assert await manager.list_queued_messages(session.id) == []
        assert len(session.run_ids) == 1
    finally:
        release.set()
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        await manager.cancel_all()


# 功能：扩展纠偏和后续输入复用真实会话队列，命令前缀默认保持字面值
# 设计：启动钩子发送两种消息，检查同一 Run 完成、队列清空及持久历史而不执行 Shell
@pytest.mark.parametrize("images", [False, True])
async def test_extension_messages_share_session_queues(tmp_path: Path, images: bool) -> None:
    bus = EventBus()
    interaction = InteractionManager(bus)
    config = CodeRookConfig()
    config.agent.extension_paths = [str(
        Path(__file__).parents[1] / "fixtures" / (
            "native_extension_image_messages.py" if images else "native_extension_messages.py"
        ),
    )]
    requests = []

    # 记录每次请求并直接回答，后续消息只能由原生外层循环继续消费
    async def chat(messages, *_args, **_kwargs):
        requests.append(deepcopy(messages))
        return LlmResponse(stop_reason="end_turn", text="Answer")

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    runner = AgentRunner(config, bus=bus, provider=provider, workspace_root=tmp_path,
                         interaction_manager=interaction)
    store = SessionStore(tmp_path / "sessions")
    runtime = RuntimeService(RuntimeStore(tmp_path / "runtime.db"), tmp_path, bus=bus)
    manager = SessionManager(store, lambda: runner, bus, runtime_service=runtime,
                             interaction_manager=interaction, workspace=tmp_path)
    session = await manager.create("chat")
    try:
        await asyncio.wait_for(manager.send_message(session.id, "Initial"), 15)
        assert len(requests) == 2
        assert "/literal-steering" in str(requests[0])
        assert "!literal-follow-up" not in str(requests[0])
        assert "!literal-follow-up" in str(requests[1])
        assert len(session.run_ids) == 1
        assert await manager.list_queued_messages(session.id) == []
        history = store.read_messages(session.id)
        assert "/literal-steering" in str(history)
        assert "!literal-follow-up" in str(history)
        if images:
            for request, expected in zip(requests, (1, 2), strict=True):
                blocks = [block for message in request
                          if isinstance(message.get("content"), list)
                          for block in message["content"] if block.get("type") == "image"]
                assert len(blocks) == expected
                for block in blocks:
                    with Image.open(io.BytesIO(base64.b64decode(block["source"]["data"]))) as image:
                        assert image.size == (2000, 100)
                    assert block["source"]["data"] in str(history)
        assert not interaction._extension_senders
    finally:
        await manager.cancel_all()


# 功能：取消时尚未送入模型的图片纠偏恢复为带附件的阻塞队列记录
# 设计：模型屏障停在请求中再发送图片，检查停止后附件可读且没有自动发起第二请求
async def test_cancel_preserves_extension_image_steering(tmp_path: Path) -> None:
    bus = EventBus()
    interaction = InteractionManager(bus)
    started = asyncio.Event()

    # 等待取消，使纠偏仍停留在待处理队列而不是已消费的历史中
    async def chat(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=chat)
    runner = AgentRunner(CodeRookConfig(), bus=bus, provider=provider, workspace_root=tmp_path,
                         interaction_manager=interaction)
    runtime = RuntimeService(RuntimeStore(tmp_path / "runtime.db"), tmp_path, bus=bus)
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: runner, bus,
                             runtime_service=runtime, interaction_manager=interaction,
                             workspace=tmp_path)
    session = await manager.create("chat")
    sending = asyncio.create_task(manager.send_message(session.id, "Initial"))
    try:
        await asyncio.wait_for(started.wait(), 5)
        image = io.BytesIO()
        Image.new("RGB", (8, 4), "blue").save(image, format="PNG")
        await manager._extension_hosts[session.id].api.send_user_message([
            {"type": "text", "text": "/literal-image"},
            {"type": "image", "data": base64.b64encode(image.getvalue()).decode("ascii"),
             "mimeType": "image/png"},
        ], deliver_as="steer")
        await manager.cancel_run(session.run_ids[-1])
        records = await manager.list_queued_messages(session.id)
        assert len(records) == 1
        assert records[0].status == "blocked" and not records[0].expand_prompt_templates
        assert "/literal-image" in records[0].content
        assert len(records[0].attachments) == 1
        _, blocks = await manager._prepare_image_attachments(records[0].attachments)
        assert base64.b64decode(blocks[0]["source"]["data"]) == image.getvalue()
        assert provider.chat.await_count == 1
    finally:
        sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
        await manager.cancel_all()


# 功能：扩展状态跨同一会话的 Run 保留，不同会话隔离且关闭时才释放
# 设计：两会话执行三轮，检查模型实际提示计数、模块存活和清理标记
async def test_extension_state_owned_by_session(tmp_path: Path) -> None:
    bus = EventBus()
    config = CodeRookConfig()
    config.agent.extension_paths = [str(
        Path(__file__).parents[1] / "fixtures" / "native_extension_session.py",
    )]
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Answer"))
    runner = AgentRunner(config, bus=bus, provider=provider, workspace_root=tmp_path)
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: runner, bus,
                             workspace=tmp_path)
    first = await manager.create("chat")
    second = await manager.create("chat")
    marker = tmp_path / "session-extension-closed.txt"
    try:
        await manager.send_message(first.id, "First")
        host = manager._extension_hosts[first.id]
        assert host.loaded and host.modules and host.registry is None
        assert not host.api.closed and not marker.exists()
        await host.api.send_user_message("Second")
        assert manager._extension_hosts[first.id] is host
        await manager.send_message(second.id, "Separate")
        await manager.reload_resources(first.id)
        assert host.api.closed and not host.modules
        assert marker.read_text("utf-8") == "2"
        replacement = manager._extension_hosts[first.id]
        assert replacement is not host and replacement.loaded
        assert provider.chat.await_count == 3
        await replacement.api.send_user_message("After reload", deliver_as="steer")
        for request, count in zip(provider.chat.call_args_list, (1, 2, 1, 1), strict=True):
            assert f"Session count: {count}" in request.kwargs["system"]
            assert [tool["name"] for tool in request.kwargs["tool_schemas"]] == ["read"]
        await manager.close(first.id)
        assert host.api.closed and not host.modules
        assert marker.read_text("utf-8") == "1"
        assert not manager._extension_hosts[second.id].api.closed
    finally:
        await manager.cancel_all()
    assert marker.read_text("utf-8") == "1"
    assert not manager._extension_hosts


# 功能：扩展命令在首个任务前可发现，本地命令不调用模型而任务命令使用同一会话
# 设计：真实会话宿主执行两类处理器，随后重载移除扩展并检查旧命令失效
async def test_extension_commands_use_session(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(
        Path(__file__).parents[1] / "fixtures" / "native_extension_commands.py",
    )]
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Answer"))
    bus = EventBus()
    runner = AgentRunner(config, bus=bus, provider=provider, workspace_root=tmp_path)
    manager = SessionManager(SessionStore(tmp_path / "sessions"), lambda: runner, bus,
                             workspace=tmp_path)
    session = await manager.create("chat")
    try:
        await manager.prepare_extensions(session.id)
        commands = manager.input_commands(session.id)
        assert {entry["name"] for entry in commands if entry["kind"] == "extension"} == {
            "greet", "ask-agent",
        }
        assert await manager.execute_extension_command(session.id, '/greet "世界"') == 'Hello: "世界"'
        assert not session.run_ids and provider.chat.await_count == 0
        assert await manager.execute_extension_command(session.id, "/ask-agent Explain") == "Request completed"
        assert len(session.run_ids) == provider.chat.await_count == 1
        config.agent.extension_paths = []
        await manager.reload_resources(session.id)
        assert not any(entry["kind"] == "extension" for entry in manager.input_commands(session.id))
        with pytest.raises(ValueError, match="Unknown extension command"):
            await manager.execute_extension_command(session.id, "/greet again")
    finally:
        await manager.cancel_all()


# 功能：后续消息保持可序列化且持久化后才确认消费，纠偏不误领后续消息回调。
# 设计：捕获生产驱动返回的原生历史，记录两条后续消息与纠偏的落账顺序。
async def test_follow_up_callbacks_stay_outside_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    interaction = InteractionManager(bus)
    interaction.register_run("follow-up")
    interaction.steer("follow-up", "steering")
    order: list[str] = []
    queued = True

    # 记录第一条消息已被消费的时刻。
    async def first_admitted():
        order.append("ack:first")

    # 记录第二条消息已被消费的时刻。
    async def second_admitted():
        order.append("ack:second")

    # 一次性发出两条后续消息，确保分别确认且不向下一轮泄漏。
    async def follow_up():
        nonlocal queued
        if not queued:
            return []
        queued = False
        return [FollowUpMessage("first", first_admitted),
                FollowUpMessage("second", second_admitted)]

    interaction.bind_follow_up("follow-up", follow_up)
    original = driver.run_agent_loop

    # 用 JSON 序列化原生循环完整返回值，避免 Provider 转换过滤字段掩盖问题。
    async def capture(*args, **kwargs):
        result = await original(*args, **kwargs)
        json.dumps(result)
        assert all("_admitted" not in message for message in result)
        return result

    monkeypatch.setattr(driver, "run_agent_loop", capture)
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Answer"))
    transcript = MagicMock()
    transcript.restore_context_usage.return_value = None
    transcript.append_audit.return_value = 1
    snapshots = []
    transcript.append_request_snapshot.side_effect = lambda _step, snapshot: (
        snapshots.append(snapshot) or len(snapshots)
    )
    transcript.latest_request_snapshot.side_effect = lambda: snapshots[-1]
    transcript.append_user.side_effect = lambda _step, content: order.append(f"log:{content}")
    loop = AgentLoop(provider, ToolRegistry(), bus, interaction_manager=interaction)
    loop._transcript = transcript
    context = ExecutionContext(run_id="follow-up", goal="start", max_steps=4)
    await loop.run(context)
    assert context.status == "success", context.reason
    assert provider.chat.await_count == 2
    assert order == ["log:steering", "log:first", "ack:first", "log:second", "ack:second"]

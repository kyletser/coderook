from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from textual.widgets import Static

from code_rook.core.transport.socket_client import SocketClient
from code_rook.tui.app import CodeRookTuiApp
from code_rook.tui.commands import match_slash_command
from code_rook.tui.product import RunResultCard
from code_rook.tui.widgets.stream import LLMStreamBlock, ToolCallBlock


# 功能：验证新任务页无需创建会话即可刷新资源，且不触发模型请求。
# 设计：通过真实 daemon HTTP 两次读取目录并新增模板，检查实时变化及会话数量不变。
async def test_workspace_commands_without_session(
    running_daemon: subprocess.Popen[bytes], api_port: int, api_token: str,
    daemon_home: Path, model_gate: tuple[asyncio.Event, asyncio.Event],
) -> None:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{api_port}",
        headers={"Authorization": f"Bearer {api_token}"}, timeout=10,
    ) as client:
        before = (await client.get("/v1/threads")).json()
        first = await client.get("/v1/workspace/input-commands")
        assert first.status_code == 200
        assert any(entry["name"] == "brief" for entry in first.json()["input_commands"])
        (daemon_home / ".coderook" / "prompts" / "fresh.md").write_text(
            "Fresh template: $ARGUMENTS", encoding="utf-8",
        )
        second = await client.get("/v1/workspace/input-commands")
        assert second.status_code == 200
        assert any(entry["name"] == "fresh" for entry in second.json()["input_commands"])
        assert (await client.get("/v1/threads")).json() == before
        assert not model_gate[0].is_set()


# 功能：IPC 与 HTTP 共用扩展资源，接管输入不创建运行或触发模型。
# 设计：真实 daemon 连续重载、执行命令及接管输入，检查任务数和模型屏障。
async def test_reload_resources_across_transports(
    running_daemon: subprocess.Popen[bytes], free_port: int, api_port: int,
    ipc_token: str, api_token: str,
    model_gate: tuple[asyncio.Event, asyncio.Event],
) -> None:
    ipc = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await ipc.connect()
    listener = asyncio.create_task(ipc.run_event_loop())
    try:
        created = await ipc.send_command("session.create", {"mode": "chat"})
        sid = created["session_id"]
        tui = await ipc.send_command("session.reload", {"session_id": sid})
        assert any(item["name"] == "greet" for item in tui["input_commands"])
        local_command = await ipc.send_command("session.execute_command", {
            "session_id": sid, "content": "/greet local",
        })
        for command, key in [("session.send_message", "session_id"),
                             ("session.queue_message", "session_id"), ("turn.start", "thread_id")]:
            handled = await ipc.send_command(command, {key: sid, "content": "__local_input__"})
            assert handled["handled"] is True
            assert not handled.get("run_id") and not handled.get("turn_id")
        headless = await ipc.send_command("agent.run", {
            "goal": "__local_input__", "resume_session_id": sid,
        })
        assert headless["handled"] is True and not headless["run_id"]
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{api_port}",
            headers={"Authorization": f"Bearer {api_token}"}, timeout=10,
        ) as client:
            response = await client.post(f"/v1/threads/{sid}/reload")
            web_command = await client.post(f"/v1/threads/{sid}/command", json={
                "content": "/greet local",
            })
            for action in ("turns", "queue"):
                handled_web = await client.post(f"/v1/threads/{sid}/{action}", json={
                    "content": "__local_input__", "mode": "act",
                })
                assert handled_web.is_success, handled_web.text
                assert handled_web.json() == {"handled": True}
            after = await client.get(f"/v1/threads/{sid}/context")
            assert after.json()["run_count"] == 0
        assert response.status_code == 200, response.text
        assert response.json()["input_commands"] == tui["input_commands"]
        assert response.json()["run_count"] == tui["run_count"] == 0
        assert web_command.status_code == 200
        assert web_command.json() == local_command == {"message": "Hello: local"}
        assert not model_gate[0].is_set()
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await ipc.close()


# 功能：直接 Shell 通过真实 daemon 返回实际输出而不调用模型。
# 设计：IPC 提交、HTTP 回放并批准单次权限，模型端点保持关闭以检测误调用。
async def test_direct_shell_through_daemon(
    running_daemon: subprocess.Popen[bytes], free_port: int, api_port: int,
    ipc_token: str, api_token: str,
    daemon_home: Path, monkeypatch: pytest.MonkeyPatch,
    model_gate: tuple[asyncio.Event, asyncio.Event],
) -> None:
    ipc = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await ipc.connect()
    listener = asyncio.create_task(ipc.run_event_loop())
    sending = None
    try:
        sid = (await ipc.send_command("session.create", {"mode": "chat"}))["session_id"]
        sending = asyncio.create_task(ipc.send_command("session.send_message", {
            "session_id": sid, "content": "!!printf direct-daemon-result",
        }))
        records = []
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{api_port}",
            headers={"Authorization": f"Bearer {api_token}"}, timeout=20,
        ) as web:
            async with asyncio.timeout(20):
                async with web.stream("GET", f"/v1/threads/{sid}/events") as response:
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        records.append(event)
                        if event["type"] == "permission.requested":
                            await ipc.send_command("permission.respond", {
                                "session_id": sid,
                                "tool_use_id": event["payload"]["tool_use_id"],
                                "decision": "allow_once",
                            })
                        if event["type"] == "run.finished":
                            break
        await sending
        assert records[-1]["payload"]["status"] == "success", (
            records[-1]["payload"].get("result_summary")
        )
        assert "direct-daemon-result" in str(records)
        history = await ipc.send_command("session.get_history", {"session_id": sid})
        assert "direct-daemon-result" not in str(history["messages"])
        assert any(
            message.get("role") == "bashExecution"
            and message.get("output") == "direct-daemon-result"
            for message in history["display_messages"]
        )
        assert not model_gate[0].is_set()
        monkeypatch.setenv("HOME", str(daemon_home))
        monkeypatch.setenv("USERPROFILE", str(daemon_home))
        app = CodeRookTuiApp(
            "127.0.0.1", free_port, auth_token=ipc_token,
            resume_session_id=sid, continue_recent=False,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            async with asyncio.timeout(15):
                while not (app._connection and app._connection._live_subscribed):
                    await pilot.pause(0.05)
            await pilot.pause()
            output = [block for block in app.query(LLMStreamBlock)
                      if block.text == "direct-daemon-result"]
            assert len(output) == 1
            assert output[0]._finalized
    finally:
        if sending is not None:
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await ipc.close()


# 提供可控模型请求屏障，使运行中排队不依赖时间碰运气。
@pytest.fixture
def model_gate() -> tuple[asyncio.Event, asyncio.Event]:
    return asyncio.Event(), asyncio.Event()


# 功能：用本地 Anthropic SSE 假端点替代真实模型，让 daemon 仍经过正式 Provider。
# 设计：第一次请求 read，收到工具结果后给出最终文本，验证的不只是内存 Mock 调用。
@pytest.fixture
async def daemon_llm_url(
    model_gate: tuple[asyncio.Event, asyncio.Event], daemon_home: Path,
) -> AsyncIterator[str]:
    prompts = daemon_home / ".coderook" / "prompts"
    prompts.mkdir(parents=True)
    extension = Path(__file__).parents[1] / "fixtures" / "native_extension_commands.py"
    (prompts.parent / "config.toml").write_text(
        f"[agent]\nextension_paths = [{json.dumps(extension.as_posix())}]\n", encoding="utf-8",
    )
    (prompts / "brief.md").write_text(
        '---\ndescription: Summarize a topic\nargument-hint: "<topic>"\n---\n'
        "Expanded template: $ARGUMENTS", encoding="utf-8",
    )
    # 返回实际 SDK 可解析的最小流，不连接任何远程服务。
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            size = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                        if line.lower().startswith(b"content-length:"))
            request = json.loads(await reader.readexactly(size))
            model_gate[0].set()
            await model_gate[1].wait()
            observed_result = any(
                isinstance(message.get("content"), list) and any(
                    block.get("type") == "tool_result" for block in message["content"]
                ) for message in request["messages"]
            )
            observed_image = any(
                isinstance(message.get("content"), list) and any(
                    block.get("type") == "image" for block in message["content"]
                ) for message in request["messages"]
            )
            block = {"type": "text", "text": ""} if observed_result else {
                "type": "tool_use", "id": "read-1", "name": "read", "input": {},
            }
            answer = "Received queued image." if observed_image else "Read README successfully."
            delta = {"type": "text_delta", "text": answer} if observed_result else {
                "type": "input_json_delta", "partial_json": json.dumps({"path": "README.md"}),
            }
            events = [
                {"type": "message_start", "message": {
                    "id": "msg-local", "type": "message", "role": "assistant", "content": [],
                    "model": "claude-test", "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 0},
                }},
                {"type": "content_block_start", "index": 0, "content_block": block},
                {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {
                    "stop_reason": "end_turn" if observed_result else "tool_use", "stop_sequence": None,
                }, "usage": {"output_tokens": 10}},
                {"type": "message_stop"},
            ]
            body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"


# 功能：完整 TUI 输入扩展命令后显示本地结果，不触发模型或新 Run
# 设计：通过 Textual 键盘事件和真实 IPC 提交，捕获最终可见文字与 Core 会话状态
async def test_extension_command_in_complete_tui(
    running_daemon: subprocess.Popen[bytes], free_port: int, ipc_token: str,
    daemon_home: Path, monkeypatch: pytest.MonkeyPatch,
    model_gate: tuple[asyncio.Event, asyncio.Event],
) -> None:
    monkeypatch.setenv("HOME", str(daemon_home))
    monkeypatch.setenv("USERPROFILE", str(daemon_home))
    app = CodeRookTuiApp("127.0.0.1", free_port, auth_token=ipc_token, continue_recent=False)
    async with app.run_test(size=(100, 30)) as pilot:
        async with asyncio.timeout(15):
            while not any(item["name"] == "greet" for item in app._input_commands):
                await pilot.pause(0.05)
        prompt = app._prompt()
        assert prompt is not None
        prompt.text = "/greet keyboard"
        prompt.focus()
        await pilot.press("enter")
        async with asyncio.timeout(10):
            while not any("Hello: keyboard" in str(widget.render())
                          for widget in app.query(Static)):
                await pilot.pause(0.05)
        assert not prompt.text and not app._busy
        assert not model_gate[0].is_set()
        assert app._client is not None
        context = await app._client.send_command("session.context", {"session_id": app._session_id})
        assert context["run_count"] == 0


# 功能：完整 TUI 在真实 daemon 执行和重新打开会话后均展示最终正文。
# 设计：运行 Textual 消息泵与真实 IPC，检查可见 Markdown 而不是只检查完成事件。
async def test_native_answer_visible_in_complete_tui(
    running_daemon: subprocess.Popen[bytes], free_port: int, ipc_token: str,
    daemon_home: Path, monkeypatch: pytest.MonkeyPatch,
    model_gate: tuple[asyncio.Event, asyncio.Event],
) -> None:
    monkeypatch.setenv("HOME", str(daemon_home))
    monkeypatch.setenv("USERPROFILE", str(daemon_home))
    sid = None
    for restored in (False, True):
        app = CodeRookTuiApp(
            "127.0.0.1", free_port, auth_token=ipc_token,
            resume_session_id=sid, continue_recent=False,
        )
        async with app.run_test(size=(100, 30)) as pilot:
            async with asyncio.timeout(15):
                while not (
                    app._session_id and app._connection
                    and app._connection._live_subscribed
                ):
                    await pilot.pause(0.05)
            sid = app._session_id
            if not restored:
                (daemon_home / ".coderook" / "prompts" / "live.md").write_text(
                    "---\ndescription: Added while running\n---\nLive $ARGUMENTS",
                    encoding="utf-8",
                )
                reload_command = match_slash_command("/reload")
                prompt = app._prompt()
                assert reload_command is not None and prompt is not None
                await reload_command.handler(app, prompt, "/reload")
                assert any(item["name"] == "live" for item in app._input_commands)
                assert not model_gate[0].is_set()
                model_gate[1].set()
                await asyncio.wait_for(
                    app._do_send_message("Read README.md and answer briefly."), 20,
                )
            async with asyncio.timeout(15):
                while not any(
                    block.text == "Read README successfully." and block._finalized
                    for block in app.query(LLMStreamBlock)
                ):
                    await pilot.pause(0.05)
            await pilot.pause()
            answers = [block for block in app.query(LLMStreamBlock)
                       if block.text == "Read README successfully."]
            assert len(answers) == 1
            assert not app.query(".history-assistant")
            assert len(app.query(ToolCallBlock)) == 1
            assert answers[0].has_class("answer")
            assert not answers[0].has_class("collapsed")
            assert answers[0].query_one(".assistant-response").display
            assert not app.query(RunResultCard)


# 功能：停止真实运行后，未消费队列恢复进 TUI 草稿且不会自动发起新任务。
# 设计：假模型屏障停在请求中，排入消息并保留编辑中草稿，核对取消后的输入与持久队列。
async def test_cancel_restores_pending_input_in_complete_tui(
    running_daemon: subprocess.Popen[bytes], free_port: int, ipc_token: str,
    daemon_home: Path, monkeypatch: pytest.MonkeyPatch,
    model_gate: tuple[asyncio.Event, asyncio.Event],
) -> None:
    monkeypatch.setenv("HOME", str(daemon_home))
    monkeypatch.setenv("USERPROFILE", str(daemon_home))
    app = CodeRookTuiApp("127.0.0.1", free_port, auth_token=ipc_token, continue_recent=False)
    async with app.run_test(size=(100, 30)) as pilot:
        async with asyncio.timeout(15):
            while not (app._session_id and app._connection and app._connection._live_subscribed):
                await pilot.pause(0.05)
        sending = asyncio.create_task(app._do_send_message("Read README.md"))
        try:
            await asyncio.wait_for(model_gate[0].wait(), 15)
            assert app._client is not None
            await app._client.send_command("session.queue_message", {
                "session_id": app._session_id, "content": "Then explain the result",
            })
            prompt = app._prompt()
            assert prompt is not None
            prompt.text = "Also keep this draft"
            assert app._active_run_id is not None
            await asyncio.wait_for(app._do_cancel_run(app._active_run_id), 15)
            await asyncio.wait_for(sending, 5)
            assert prompt.text == "Then explain the result\n\nAlso keep this draft"
            result = await app._client.send_command("session.list_queue", {
                "session_id": app._session_id,
            })
            assert result["messages"] == []
            assert not app._busy
        finally:
            model_gate[1].set()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


# 功能：真实 daemon 的新 Python 循环提供最终回答，并且 HTTP 导航与 IPC 恢复一致。
# 设计：跨真实 TCP、HTTP、Provider SSE 与文件读取，使用隔离用户目录而不消耗 API 费用。
@pytest.mark.parametrize("with_follow_up", [False, True, "image", "skill", "template", "alias"])
async def test_native_answer_and_navigation_across_frontend_transports(
    running_daemon: subprocess.Popen[bytes], free_port: int, api_port: int,
    ipc_token: str, api_token: str, daemon_home: Path,
    model_gate: tuple[asyncio.Event, asyncio.Event], with_follow_up: bool | str,
) -> None:
    ipc = SocketClient("127.0.0.1", free_port, auth_token=ipc_token)
    await ipc.connect()
    listener = asyncio.create_task(ipc.run_event_loop())
    try:
        created = await ipc.send_command("session.create", {"mode": "chat"})
        sid = created["session_id"]
        sending = asyncio.create_task(ipc.send_command("session.send_message", {
            "session_id": sid, "content": "Read README.md and answer briefly.",
        }))
        await asyncio.wait_for(model_gate[0].wait(), 15)
        if with_follow_up:
            attachments = []
            if with_follow_up == "image":
                png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (2).to_bytes(4, "big") * 2
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{api_port}",
                    headers={"Authorization": f"Bearer {api_token}"}, timeout=10,
                ) as upload:
                    uploaded = await upload.post("/v1/artifacts/images", json={
                        "data_base64": base64.b64encode(png).decode("ascii"),
                    })
                    assert uploaded.status_code == 201, uploaded.text
                    attachments.append(uploaded.json())
            await ipc.send_command("session.queue_message", {
                "session_id": sid,
                "content": ("/skill:summarize " if with_follow_up == "skill" else
                            "/summarize " if with_follow_up == "alias" else
                            "/brief " if with_follow_up == "template" else "")
                + "Now give a brief follow-up.",
                "attachments": attachments,
            })
        model_gate[1].set()
        sent = await sending
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{api_port}",
            headers={"Authorization": f"Bearer {api_token}"}, timeout=10,
        ) as web:
            http_context = (await web.get(f"/v1/threads/{sid}/context")).json()
            ipc_context = await ipc.send_command("session.context", {"session_id": sid})
            assert http_context["input_commands"] == ipc_context["input_commands"]
            names = {command["name"] for command in http_context["input_commands"]}
            assert {"brief", "skill:summarize"} <= names
            brief = next(item for item in http_context["input_commands"] if item["name"] == "brief")
            assert brief["description"] == "Summarize a topic"
            assert brief["argument_hint"] == "<topic>"
            records = []
            async with asyncio.timeout(30):
                async with web.stream("GET", f"/v1/threads/{sid}/events") as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            records.append(event)
                            if event["type"] == "run.finished":
                                break
            assert any(event["type"] == "run.finished" for event in records), records
            history = await ipc.send_command("session.get_history", {"session_id": sid})
            assert "Read README successfully." in str(history["messages"])
            assert any(event["type"] == "agent.message" for event in records)
            assert sent["run_id"]
            if with_follow_up:
                assert "Now give a brief follow-up." in str(history["messages"])
                assert sum(event["type"] == "run.started" for event in records) == 1
                queue = await ipc.send_command("session.list_queue", {"session_id": sid})
                assert queue["messages"] == []
                if with_follow_up in {"skill", "alias"}:
                    assert '<skill name="summarize"' in str(history["messages"])
                    assert "References are relative to" in str(history["messages"])
                if with_follow_up == "template":
                    assert "Expanded template: Now give a brief follow-up." in str(
                        history["messages"]
                    )
                if with_follow_up == "image":
                    assert "Received queued image." in str(history["messages"])
                    assert any(
                        isinstance(message["content"], list) and any(
                            block.get("type") == "image" for block in message["content"]
                        ) for message in history["messages"]
                    )
            tree = (await web.get(f"/v1/threads/{sid}/tree")).json()["entries"]
            target = next(entry["seq"] for entry in tree if "Read README.md" in entry["preview"])
            navigated = await web.post(f"/v1/threads/{sid}/navigate", json={"target_seq": target})
            assert navigated.status_code == 200, navigated.text
            assert navigated.json()["editor_text"] == "Read README.md and answer briefly."
            restored = await ipc.send_command("session.get_history", {"session_id": sid})
            assert restored["messages"] == []
            assert (daemon_home / ".coderook" / "sessions" / sid / "thread.jsonl").exists()
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await ipc.close()

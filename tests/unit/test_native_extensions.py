import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import AnyHttpUrl

from code_rook.core.agent_runtime.extensions import ExtensionHost
from code_rook.core.agent_runtime.providers import ExtensionProvider
from code_rook.core.agent_runtime.tools import ReadTool
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.events import ExtensionUiUpdatedEvent, RunFinishedEvent
from code_rook.core.compact.compactor import Compactor
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.factory import create_provider_for_resolved_route
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.route_registry import ResolvedRoute
from code_rook.core.llm.routes import ProviderRoute
from code_rook.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from code_rook.core.runner import AgentRunner
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.workspace import WorkspaceBoundary

FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension.py"
LIFECYCLE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_lifecycle.py"
MESSAGE_END_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_message_end.py"
TREE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_tree.py"
COMPACTION_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_compaction.py"
RESOURCES_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_resources.py"
USER_BASH_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_user_bash.py"
PROVIDER_FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_extension_provider.py"
PROVIDER_HOOKS_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "native_extension_provider_hooks.py"
)


# 功能：Python 扩展的声明式 UI API 同步更新会话投影并广播给共享前端。
# 设计：使用真实 SessionManager 和 EventBus 调用全部状态类 API，核对投影与事件而不启动模型。
async def test_extension_ui_contributions_reach_shared_frontends(tmp_path: Path) -> None:
    bus = EventBus()
    seen: list[ExtensionUiUpdatedEvent] = []

    # 收集扩展 UI 事件并忽略同一总线上的其他生命周期事件。
    async def observe(event) -> None:
        if isinstance(event, ExtensionUiUpdatedEvent):
            seen.append(event)

    bus.subscribe(observe)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: AgentRunner(CodeRookConfig(), provider=MagicMock(), workspace_root=tmp_path),
        bus,
        workspace=tmp_path,
    )
    session = await manager.create("chat", "UI")
    host = await manager.prepare_extensions(session.id)
    assert host is not None

    host.api.set_status("branch", "main")
    host.api.set_working_message("Indexing")
    host.api.set_working_visible(True)
    host.api.set_hidden_thinking_label("Internal work")
    host.api.set_widget("help", ["line one", "line two"], placement="above")
    host.api.set_title("Project · CodeRook")
    host.api.set_tools_expanded(True)
    host.api.set_editor_text("review this")
    host.api.paste_to_editor(" now")
    await asyncio.sleep(0)

    context = manager.context_info(session.id)["extension_ui"]
    assert context["statuses"] == {"branch": "main"}
    assert context["working_message"] == "Indexing"
    assert context["widgets"]["help"] == {
        "content": "line one\nline two",
        "placement": "above",
    }
    assert context["title"] == "Project · CodeRook"
    assert context["tools_expanded"] is True
    assert [event.kind for event in seen] == [
        "status",
        "working_message",
        "working_visible",
        "hidden_thinking_label",
        "widget",
        "title",
        "tools_expanded",
        "editor_text",
        "editor_insert",
    ]
    assert all(event.session_id == session.id for event in seen)

    host.api.set_status("branch", None)
    host.api.set_widget("help", None)
    assert manager.context_info(session.id)["extension_ui"]["statuses"] == {}
    assert manager.context_info(session.id)["extension_ui"]["widgets"] == {}


# 功能：会话所属 Python 扩展可持久化私有状态、重命名会话并标记历史条目。
# 设计：通过真实 SessionManager 绑定 API，验证模型历史不含私有条目且树标签和标题立即可见。
async def test_extension_session_metadata_actions_are_native_and_persistent(tmp_path: Path) -> None:
    config = CodeRookConfig()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: AgentRunner(config, provider=MagicMock(), workspace_root=tmp_path),
        EventBus(),
        workspace=tmp_path,
    )
    session = await manager.create("chat", "Before")
    host = await manager.prepare_extensions(session.id)
    assert host is not None

    sequence = host.api.append_entry("counter", {"value": 3})
    host.api.set_label(str(sequence), "checkpoint")
    await host.api.set_session_name("After")
    await host.api.set_thinking_level("high")

    assert host.api.get_session_name() == "After"
    assert manager.get_session(session.id).title == "After"
    assert host.api.get_thinking_level() == "high"
    assert manager.get_session(session.id).thinking_level == "high"
    assert store.read_messages(session.id) == []
    entry = next(item for item in store.session_tree(session.id) if item["seq"] == sequence)
    assert entry["type"] == "extension.entry"
    assert entry["label"] == "checkpoint"
    host.api.set_label(str(sequence), None)
    assert store.read_entry_labels(session.id) == {}
    assert host.api.is_idle()
    await host.api.wait_for_idle()
    assert not await host.api.abort()


# 功能：Python 扩展运行控制方法转交所属会话服务并返回实际结果。
# 设计：用显式异步回调验证等待、停止、压缩和重载的参数及返回值，不启动模型。
async def test_extension_runtime_controls_are_awaitable(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "runtime-controls")
    calls: list[tuple[str, str]] = []

    async def wait_for_idle() -> None:
        calls.append(("wait", ""))

    async def abort() -> bool:
        calls.append(("abort", ""))
        return True

    async def compact(focus: str) -> dict[str, str]:
        calls.append(("compact", focus))
        return {"summary": "kept"}

    async def reload_resources() -> dict[str, bool]:
        calls.append(("reload", ""))
        return {"reloaded": True}

    host.api.idle_getter = lambda: False
    host.api.idle_waiter = wait_for_idle
    host.api.run_aborter = abort
    host.api.compaction_requester = compact
    host.api.resource_reloader = reload_resources

    assert not host.api.is_idle()
    await host.api.wait_for_idle()
    assert await host.api.abort()
    assert await host.api.compact("preserve constraints") == {"summary": "kept"}
    assert await host.api.reload() == {"reloaded": True}
    assert calls == [
        ("wait", ""),
        ("abort", ""),
        ("compact", "preserve constraints"),
        ("reload", ""),
    ]


# 功能：Python 扩展的选择、确认和输入 API 使用与 Agent 提问相同的交互通道。
# 设计：在空闲会话监听真实问题事件并即时作答，核对三种交互的返回语义和 session 归属。
async def test_extension_interactive_prompts_use_shared_question_channel(tmp_path: Path) -> None:
    bus = EventBus()
    from code_rook.core.interaction import InteractionManager

    interaction = InteractionManager(bus)
    seen: list[dict[str, object]] = []

    async def answer(event) -> None:
        if event.type != "user_question.asked":
            return
        payload = event.model_dump(mode="json")
        seen.append(payload)
        responses = ["B", "Yes", "typed value"]
        assert interaction.answer(event.question_id, responses[len(seen) - 1])

    bus.subscribe(answer)
    config = CodeRookConfig()
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: AgentRunner(config, provider=MagicMock(), workspace_root=tmp_path),
        bus,
        interaction_manager=interaction,
        workspace=tmp_path,
    )
    session = await manager.create("chat", "Prompts")
    host = await manager.prepare_extensions(session.id)
    assert host is not None

    assert await host.api.select("Choose", ["A", "B"]) == "B"
    assert await host.api.confirm("Confirm", "Continue?")
    assert await host.api.input("Name", "Type a value") == "typed value"
    assert all(event["session_id"] == session.id for event in seen)
    assert [event["options"] for event in seen] == [["A", "B"], ["Yes", "No"], []]


# 功能：扩展自定义消息可以仅追加、显示通知或在空闲会话直接启动模型 Turn。
# 设计：通过真实 SessionManager 和原生 Runner 核对 Ledger 来源、展示投影及 trigger_turn 请求历史。
async def test_extension_custom_messages_follow_native_session_semantics(tmp_path: Path) -> None:
    bus = EventBus()
    provider = MagicMock()
    provider.context_window = 128_000
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done"))
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: AgentRunner(CodeRookConfig(), provider=provider, workspace_root=tmp_path),
        bus,
        workspace=tmp_path,
    )
    session = await manager.create("chat", "Custom messages")
    host = await manager.prepare_extensions(session.id)
    assert host is not None

    await host.api.send_message({
        "custom_type": "status",
        "content": "background state",
        "display": False,
    })
    await host.api.notify("visible notice", notification_type="warning")
    assert provider.chat.await_count == 0
    display_messages = store.derive_messages(session.id, display=True)
    assert display_messages == []

    await host.api.send_message({
        "custom_type": "task",
        "content": [{"type": "text", "text": "start this task"}],
        "display": True,
    }, trigger_turn=True)
    assert provider.chat.await_count == 1
    sent_messages = provider.chat.await_args.kwargs["messages"]
    assert "background state" in str(sent_messages)
    assert "start this task" in str(sent_messages)
    events = store.read_session_events(session.id)
    assert sum(
        event.payload.get("source", {}).get("kind") == "extension"
        for event in events
        if event.type == "input.admitted"
    ) == 2


# 功能：Python 扩展能读取实际 System Prompt 和最近 Provider 上下文用量。
# 设计：用带固定 usage 的假 Provider 跑真实原生循环，避免把静态配置误当运行时占用。
async def test_extension_exposes_effective_prompt_and_context_usage(tmp_path: Path) -> None:
    config = CodeRookConfig()
    provider = MagicMock()
    provider.context_window = 1_000
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text="done",
        usage=UsageStats(input_tokens=100, output_tokens=20, cache_read_input_tokens=30),
    ))
    runner = AgentRunner(config, provider=provider, workspace_root=tmp_path)
    host = runner.create_extension_host("run-introspection")
    await host.initialize()

    outcome = await runner.run_and_capture("Answer directly", extension_host=host)

    assert outcome.status == "success"
    assert "Language Policy" in host.api.get_system_prompt()
    assert host.api.get_context_usage() == {
        "tokens": 150,
        "contextWindow": 1_000,
        "percent": 15.0,
    }
    await host.close()


# 功能：Python 扩展能查询命令目录并以参数数组执行工作区子进程。
# 设计：使用当前 Python 解释器回显 cwd 和 stderr，跨平台验证无 Shell 拼接的完整返回结构。
async def test_extension_commands_and_exec_are_available_without_node(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "extension-exec")
    host.api.register_command("hello", lambda arguments: arguments, description="Say hello")
    result = await host.api.exec(
        sys.executable,
        ["-c", "import os,sys; print(os.path.basename(os.getcwd())); print('warn', file=sys.stderr)"],
    )

    assert host.api.get_commands() == [{"name": "hello", "description": "Say hello"}]
    assert result == {
        "stdout": f"{tmp_path.name}\r\n" if sys.platform == "win32" else f"{tmp_path.name}\n",
        "stderr": "warn\r\n" if sys.platform == "win32" else "warn\n",
        "code": 0,
        "killed": False,
    }


# 功能：扩展启动后注册的工具与命令无需 reload 即刻进入当前会话。
# 设计：先绑定空宿主，再动态覆盖工具和增加命令，最后卸载并核对注册表完整恢复。
async def test_extension_runtime_registration_is_immediate(tmp_path: Path) -> None:
    class DynamicTool(BaseTool):
        name = "dynamic_now"
        description = "Registered after startup"
        input_schema = {"type": "object", "properties": {}}
        side_effect = ToolSideEffect.NONE

        # 返回动态工具标记以证明实际注册实例可调用。
        async def invoke(self, params):
            return ToolResult("dynamic")

    registry = ToolRegistry()
    before = registry.canonical_catalog_json()
    host = ExtensionHost([], tmp_path, "dynamic-registration")
    await host.load(registry)

    host.api.register_tool(DynamicTool())
    host.api.register_command("dynamic-command", lambda value: f"now:{value}")

    assert host.api.get_active_tools() == ["dynamic_now"]
    assert (await registry.get("dynamic_now").invoke({})).content == "dynamic"  # type: ignore[union-attr]
    assert await host.execute_command("dynamic-command", "yes") == "now:yes"
    await host.close()
    assert registry.canonical_catalog_json() == before


# 功能：Python 扩展接收 Pi 原生 Agent、Turn、Message 和 Tool 生命周期事件。
# 设计：真实 Runner 执行一次工具调用再回答，按日志顺序验证零起始轮次和完整负载。
async def test_extension_receives_native_agent_lifecycle(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(LIFECYCLE_FIXTURE)]
    (tmp_path / "sample.txt").write_text("lifecycle", encoding="utf-8")
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=[
        LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(id="read-life", name="read", input={"path": "sample.txt"}),
        ]),
        LlmResponse(stop_reason="end_turn", text="Lifecycle completed"),
    ])
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-life", "chat", "active", "Lifecycle", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Read the sample")
    outcome = await AgentRunner(
        config, provider=provider, workspace_root=tmp_path,
    ).run_and_capture("Read the sample", session=session, store=store)
    assert outcome.status == "success"
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [event["type"] for event in events]
    assert kinds[0] == "agent_start" and kinds[-2:] == ["agent_end", "agent_settled"]
    assert [event["turnIndex"] for event in events if event["type"] == "turn_start"] == [0, 1]
    assert [event["turnIndex"] for event in events if event["type"] == "turn_end"] == [0, 1]
    assert "tool_execution_start" in kinds and "tool_execution_end" in kinds
    assert any(
        event["type"] == "message_end" and event["message"]["role"] == "toolResult"
        for event in events
    )
    assert events[-2]["messages"][-1]["role"] == "assistant"
    assert events[-1] == {"type": "agent_settled", "run_id": events[-1]["run_id"]}


# 功能：模型异常退出时扩展仍收到唯一 agent_end，能够可靠释放本轮状态。
# 设计：让 Provider 在首轮直接失败，检查 finally 补发事件且不伪造消息。
async def test_extension_agent_end_survives_model_failure(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(LIFECYCLE_FIXTURE)]
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("provider crashed"))
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-failed-life", "chat", "active", "Failure", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Fail now")
    outcome = await AgentRunner(
        config, provider=provider, workspace_root=tmp_path,
    ).run_and_capture("Fail now", session=session, store=store)
    assert outcome.status == "failed"
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in events].count("agent_start") == 1
    assert [event["type"] for event in events].count("agent_end") == 1
    assert [event["type"] for event in events].count("agent_settled") == 1
    assert events[-2] == {
        "type": "agent_end", "messages": [], "run_id": events[-2]["run_id"],
    }
    assert events[-1] == {"type": "agent_settled", "run_id": events[-1]["run_id"]}


# 功能：message_end 的链式替换成为权威结果并持久进入会话历史。
# 设计：真实 Runner 接收 Provider 原文，断言两层扩展替换后的正文贯穿结果和 Ledger。
async def test_extension_message_end_replaces_authoritative_message(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(MESSAGE_END_FIXTURE)]
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Original"))
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-replace", "chat", "active", "Replace", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Replace the answer")
    outcome = await AgentRunner(
        config, provider=provider, workspace_root=tmp_path,
    ).run_and_capture("Replace the answer", session=session, store=store)
    assert outcome.status == "success"
    assert outcome.result == "Replaced by extension twice"
    messages = store.read_messages(session.id)
    assert messages[-1]["content"] == [
        {"type": "text", "text": "Replaced by extension twice"},
    ]
    assert "Original" not in str(messages[-1])


# 功能：会话创建、重命名、资源重载和关闭向会话所属扩展发送生命周期事件。
# 设计：复用同一真实 SessionManager 顺序执行操作，检查重载前后宿主写入同一日志。
async def test_extension_receives_session_lifecycle(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(LIFECYCLE_FIXTURE)]
    runner = AgentRunner(config, provider=MagicMock(), workspace_root=tmp_path)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: runner, EventBus(), workspace=tmp_path,
    )
    session = await manager.create("chat", "Initial")
    await manager.rename(session.id, "Renamed")
    await manager.reload_resources(session.id)
    reloaded = manager._extension_hosts[session.id]
    assert reloaded.api.message_sender is not None
    assert reloaded.api.custom_message_sender is not None
    assert reloaded.api.notification_sender is not None
    assert reloaded.api.ui_setter is not None
    assert reloaded.api.model_setter is not None
    assert reloaded.api.question_asker is not None
    await manager.close(session.id)
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    selected = [event for event in events if event["type"].startswith("session_")]
    assert [(event["type"], event.get("reason"), event.get("name")) for event in selected] == [
        ("session_start", "new", None),
        ("session_info_changed", None, "Renamed"),
        ("session_shutdown", "reload", None),
        ("session_start", "reload", None),
        ("session_shutdown", "quit", None),
    ]


# 功能：扩展发现的 Skill、Prompt 和主题进入当前会话，并在重载后清除旧资源。
# 设计：从扩展源码相对路径加载真实资源，贯穿命令目录、输入展开和上下文投影后再移除扩展。
async def test_extension_discovered_resources_reach_session(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(RESOURCES_FIXTURE)]
    provider = MagicMock()
    provider.context_window = 128_000
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="done"))
    runner = AgentRunner(config, provider=provider, workspace_root=tmp_path)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: runner, EventBus(), workspace=tmp_path,
    )
    session = await manager.create("chat", "Resources")
    commands = {command["name"]: command for command in manager.input_commands(session.id)}
    assert commands["skill:discovered"]["kind"] == "skill"
    assert commands["discovered-prompt"]["kind"] == "template"
    assert "extension resource" in manager._expand_native_skill(
        session.id, "/skill:discovered auth"
    )
    assert manager._expand_native_skill(
        session.id, "/discovered-prompt auth.py"
    ) == "Review auth.py using the discovered prompt."
    context = manager.context_info(session.id)
    assert context["theme_paths"] == [str(
        RESOURCES_FIXTURE.parent / "extension_resources" / "themes" / "light.json"
    )]
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-resource-events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["reason"] for event in events] == ["startup"]
    await manager.send_message(session.id, "/skill:discovered auth")
    request = provider.chat.call_args.kwargs
    assert "Use an extension-discovered skill" in request["system"]
    assert "Inspect the requested target with the extension resource." in str(
        request["messages"]
    )

    config.agent.extension_paths = []
    await manager.reload_resources(session.id)
    names = {command["name"] for command in manager.input_commands(session.id)}
    assert "skill:discovered" not in names
    assert "discovered-prompt" not in names
    assert manager.context_info(session.id)["theme_paths"] == []
    await manager.cancel_all()


# 功能：用户 ! 命令可由 Python 扩展完整接管，并保留 !! 的上下文排除标记。
# 设计：使用操作系统不存在的虚拟命令完成两次真实 Runner 调用，排除误走本地 Shell 的可能。
async def test_extension_intercepts_user_bash(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(USER_BASH_FIXTURE)]
    runner = AgentRunner(config, provider=MagicMock(), workspace_root=tmp_path)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-bash", "chat", "active", "Bash", "now", "now")
    store.write_meta(session)
    host = runner.create_extension_host("")
    await host.initialize()
    from code_rook.core.agent_runtime.user_shell import UserShellCommand

    first = await runner.run_user_shell(
        UserShellCommand("virtual-command"), run_id="bash-one",
        session=session, store=store, extension_host=host,
    )
    second = await runner.run_user_shell(
        UserShellCommand("virtual-command", include_in_context=False), run_id="bash-two",
        session=session, store=store, extension_host=host,
    )
    assert first.status == second.status == "success"
    assert first.result == second.result == "virtual output"
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-user-bash.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["excludeFromContext"] for event in events] == [False, True]
    assert all(event["cwd"] == str(tmp_path) for event in events)
    completed = [
        event for event in store.read_session_events(session.id)
        if event.type == "user.shell_completed"
    ]
    assert [event.payload["exclude_from_context"] for event in completed] == [False, True]
    await host.close()


# 功能：会话分叉向源扩展发送可取消事件，并让新会话收到带来源的启动事件。
# 设计：真实创建一次 fork 后检查两个会话宿主的共享日志，再直接验证 cancel 短路。
async def test_extension_session_fork_lifecycle_and_cancel(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(LIFECYCLE_FIXTURE)]
    runner = AgentRunner(config, provider=MagicMock(), workspace_root=tmp_path)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"), lambda: runner, EventBus(), workspace=tmp_path,
    )
    source = await manager.create("chat", "Source")
    forked = await manager.fork(source.id, "Forked")
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["type"] == "session_before_fork" for event in events)
    fork_start = next(
        event for event in events
        if event["type"] == "session_start" and event["reason"] == "fork"
    )
    assert fork_start["previousSessionFile"].endswith(f"{source.id}\\thread.jsonl")
    host = manager._extension_hosts[source.id]
    called = []
    host.api.on("session_before_fork", lambda event: {"cancel": True})
    host.api.on("session_before_fork", lambda event: called.append(event))
    with pytest.raises(Exception, match="cancelled by extension"):
        await manager.fork(source.id, "Blocked")
    assert called == []
    assert manager._get_session(forked.id).title == "Forked"
    await manager.cancel_all()


# 功能：分支扩展可取消导航或直接提供摘要，并在成功后收到最终叶节点。
# 设计：真实追加两条历史后导航到用户节点，确保本地摘要跳过 Provider 且标签持久化。
async def test_extension_controls_session_tree_navigation(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(TREE_FIXTURE)]
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=AssertionError("extension summary must skip provider"))
    runner = AgentRunner(config, provider=provider, workspace_root=tmp_path)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, lambda: runner, EventBus(), provider=provider, workspace=tmp_path,
    )
    session = await manager.create("chat", "Tree")
    store.append_message(session.id, "user", "Original question")
    target = store.read_session_events(session.id)[-1].seq
    store.append_message(session.id, "assistant", "Old answer")
    result = await manager.navigate_tree(session.id, target, summarize=True)
    assert result["label"] == "from-extension"
    assert provider.chat.await_count == 0
    selected = store.read_session_events(session.id)[-1]
    assert selected.type == "session.branch_selected"
    assert selected.payload["summary"] == "Extension branch summary"
    assert selected.payload["label"] == "from-extension"
    lifecycle = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-tree.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert lifecycle[0]["type"] == "session_before_tree"
    assert lifecycle[0]["preparation"]["targetId"] == str(target)
    assert lifecycle[0]["preparation"]["entriesToSummarize"]
    assert lifecycle[1]["type"] == "session_tree"
    assert lifecycle[1]["newLeafId"] is None
    assert lifecycle[1]["fromExtension"] is True
    host = manager._extension_hosts[session.id]
    host.api.handlers.insert(0, ("session_before_tree", lambda event: {"cancel": True}))
    before = (store.session_dir(session.id) / "thread.jsonl").read_bytes()
    with pytest.raises(Exception, match="cancelled by extension"):
        await manager.navigate_tree(session.id, target)
    assert (store.session_dir(session.id) / "thread.jsonl").read_bytes() == before
    await manager.cancel_all()


# 功能：扩展提供的会话摘要走正式 append-only 压缩提交并跳过模型调用。
# 设计：手动压缩真实 Session Ledger，检查生命周期顺序、扩展详情与投影后的摘要。
async def test_extension_supplies_session_compaction(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(COMPACTION_FIXTURE)]
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=AssertionError("extension compaction skips provider"))
    runner = AgentRunner(config, provider=provider, workspace_root=tmp_path)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, lambda: runner, EventBus(), provider=provider, workspace=tmp_path,
    )
    session = await manager.create("chat", "Compact")
    store.append_message(session.id, "user", "Long task context")
    store.append_message(session.id, "assistant", "Completed some work")
    result = await manager.compact(session.id)
    assert provider.chat.await_count == 0
    assert result.quality_score == 1.0
    assert "Extension supplied checkpoint" in str(store.read_messages(session.id))
    events = [
        __import__("json").loads(line)
        for line in (tmp_path / "extension-compaction.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [event["type"] for event in events] == [
        "session_before_compact", "session_compact",
    ]
    assert events[0]["reason"] == events[1]["reason"] == "manual"
    assert events[1]["fromExtension"] is True
    assert events[1]["compactionEntry"]["details"] == {"owner": "extension"}
    ledger = store.read_session_events(session.id)
    assert any(event.type == "context.compaction.committed" for event in ledger)
    await manager.cancel_all()


# 功能：扩展取消压缩时原始 Ledger 不变，并收到明确的 aborted 失败事件。
# 设计：在空扩展宿主动态注册取消处理器，比较操作前后日志字节且禁止 Provider 调用。
async def test_extension_cancels_session_compaction(tmp_path: Path) -> None:
    config = CodeRookConfig()
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=AssertionError("cancelled compaction skips provider"))
    runner = AgentRunner(config, provider=provider, workspace_root=tmp_path)
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store, lambda: runner, EventBus(), provider=provider, workspace=tmp_path,
    )
    session = await manager.create("chat", "Cancel compact")
    store.append_message(session.id, "user", "Keep this history")
    before = (store.session_dir(session.id) / "thread.jsonl").read_bytes()
    host = manager._extension_hosts[session.id]
    failures = []
    host.api.on("session_before_compact", lambda event: {"cancel": True})
    host.api.on("session_compact_failed", lambda event: failures.append(event))
    with pytest.raises(Exception, match="compaction failed"):
        await manager.compact(session.id)
    assert provider.chat.await_count == 0
    assert (store.session_dir(session.id) / "thread.jsonl").read_bytes() == before
    assert len(failures) == 1
    assert failures[0]["aborted"] is True
    assert failures[0]["reason"] == "manual"
    await manager.cancel_all()


# 功能：上下文溢出压缩向扩展标明自动重试语义并采用其摘要。
# 设计：直接驱动生产 Compactor 的 overflow 路径，避免依赖大模型 Token 阈值猜测。
async def test_extension_compaction_reports_overflow_retry(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "overflow-run")
    observed = []

    # 为溢出压缩提供确定性结果并保存前后事件。
    def before(event: dict) -> dict:
        observed.append(event)
        return {"compaction": {"summary": "Overflow checkpoint"}}

    host.api.on("session_before_compact", before)
    host.api.on("session_compact", lambda event: observed.append(event))
    context = ExecutionContext(
        "overflow-run", "Continue", 1,
        prefill_messages=[{"role": "user", "content": "x" * 200}],
    )
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=AssertionError("extension skips provider"))
    result = await Compactor(
        EventBus(), tmp_path / "session", "session",
        keep_recent_tokens=1, lifecycle=host.emit_session_event,
    ).compact(context, provider, trigger="overflow")
    assert result is not None and result.strategy == "extension"
    assert context.messages == result.messages
    assert [event["type"] for event in observed] == [
        "session_before_compact", "session_compact",
    ]
    assert all(event["reason"] == "overflow" for event in observed)
    assert all(event["willRetry"] is True for event in observed)
    assert provider.chat.await_count == 0
    await host.close()


# 功能：用户 Python 扩展的工具进入真实请求和执行管线，运行结束调用清理函数
# 设计：用模型替身发起扩展工具调用，检查下一请求的结果与卸载后的模块表
async def test_extension_tool_in_real_runner(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(FIXTURE)]
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=[
        LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(id="extension-call", name="extension_greeting", input={}),
        ]),
        LlmResponse(stop_reason="end_turn", text="Extension completed"),
    ])
    before = {name for name in sys.modules if name.startswith("_coderook_extension_")}
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-extension", "chat", "active", "Extension", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Use the greeting tool")
    result = await AgentRunner(config, provider=provider, workspace_root=tmp_path).run_and_capture(
        "Use the greeting tool", session=session, store=store,
    )
    assert result.status == "success"
    assert "extension_greeting" in str(provider.chat.call_args_list[0])
    assert "Use the extension greeting when requested." in str(provider.chat.call_args_list[0])
    assert "A deterministic local greeting" in str(provider.chat.call_args_list[0])
    assert "Only greet when explicitly requested." in str(provider.chat.call_args_list[0])
    assert "Greeting context from extension" in str(provider.chat.call_args_list[0])
    assert "Hello from a Python extension" in str(provider.chat.call_args_list[1])
    assert "[extension processed]" in str(provider.chat.call_args_list[1])
    assert "display-metadata" not in str(provider.chat.call_args_list[1])
    assert "iVBORw0KGgo" in str(provider.chat.call_args_list[1])
    assert (tmp_path / "extension-closed.txt").read_text() == "closed"
    assert (tmp_path / "extension-finished.txt").read_text() == "success"
    assert {name for name in sys.modules if name.startswith("_coderook_extension_")} == before
    reopened = SessionStore(tmp_path / "sessions")
    assert "Greeting context from extension" in str(reopened.read_messages(session.id))
    assert "iVBORw0KGgo" in str(reopened.read_messages(session.id))
    assert "Greeting context from extension" not in str(
        reopened.derive_messages(session.id, display=True),
    )
    injected = next(event for event in reopened.read_session_events(session.id)
                    if event.payload.get("source", {}).get("kind") == "extension")
    assert injected.payload["details"] == {"version": 1}
    assert injected.payload["source"]["custom_type"] == "greeting-context"
    visible = next(message for message in reopened.derive_messages(session.id, display=True)
                   if message["role"] == "custom")
    assert visible["content"] == "Visible extension note"
    assert visible["custom_type"] == "greeting-note"
    run_events = next((store.runs_dir(session.id)).glob("*/events.jsonl")).read_text("utf-8")
    assert visible["native_message_id"] in run_events
    assert "display-metadata" in run_events


# 功能：扩展装载失败后仍释放之前已注册的资源并移除临时模块
# 设计：先载入正常扩展再读取不存在文件，检查半装载场景的清理效果
async def test_extension_partial_load_cleanup(tmp_path: Path) -> None:
    host = ExtensionHost([str(FIXTURE), str(tmp_path / "missing.py")], tmp_path, "partial")
    try:
        with pytest.raises(FileNotFoundError):
            await host.load(ToolRegistry())
    finally:
        await host.close()
    assert not host.modules
    assert not host.api.tools
    assert (tmp_path / "extension-closed.txt").is_file()


# 功能：扩展事件只观察当前运行且独立复制，撤销和关闭后不再收到事件
# 设计：共享总线发布两个运行并让首个回调修改数据和抛错，检查其他观察者不受影响
async def test_extension_event_scope_and_disposal(tmp_path: Path) -> None:
    bus = EventBus()
    host = ExtensionHost([], tmp_path, "ours")
    observed = []

    # 模拟有缺陷的扩展尝试修改事件后抛出异常
    def broken(event):
        event["status"] = "changed"
        raise ValueError("observer failed")

    host.api.on("run.*", broken)
    remove = host.api.on("run.finished", observed.append)
    await host.load(ToolRegistry(), bus)
    event = RunFinishedEvent(run_id="ours", status="success", steps=1, ts="now")
    await bus.publish(event.model_copy(update={"run_id": "other"}))
    await bus.publish(event)
    assert len(observed) == 1
    assert observed[0]["status"] == event.status == "success"
    remove()
    remove()
    await bus.publish(event)
    assert len(observed) == 1
    await host.close()
    assert not bus._subscribers
    with pytest.raises(RuntimeError, match="ended"):
        host.api.on("*", observed.append)


# 功能：扩展清理被取消时仍撤销模块和其他资源，并保留任务取消语义
# 设计：在最后注册的清理函数抛出取消异常，验证逆序清理不会提前终止
async def test_extension_cancelled_cleanup(tmp_path: Path) -> None:
    host = ExtensionHost([str(FIXTURE)], tmp_path, "cancel")
    await host.load(ToolRegistry())
    modules = list(host.modules)

    # 在资源清理期间模拟任务取消
    async def cancel():
        raise asyncio.CancelledError()

    host.api.on_shutdown(cancel)
    with pytest.raises(asyncio.CancelledError):
        await host.close()
    assert all(name not in sys.modules for name in modules)
    assert (tmp_path / "extension-closed.txt").read_text() == "closed"
    await host.close()


# 功能：启动钩子按顺序传递系统提示且仅影响本次运行，允许显式空提示
# 设计：混合同步和异步钩子并修改图片副本，检查链式结果和原始输入隔离
async def test_extension_startup_prompt_chain(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "start")
    images = [{"type": "image", "source": {"data": "original"}}]

    # 修改局部图片副本并为下一钩子追加提示
    def first(event):
        event["images"][0]["source"]["data"] = "modified"
        return {"system_prompt": event["system_prompt"] + " first"}

    # 验证链式上下文后返回显式空提示
    async def second(event):
        assert event["system_prompt"] == "base first"
        assert event["images"] == images
        return {"system_prompt": ""}

    host.api.on("before_agent_start", first)
    host.api.on("before_agent_start", second)
    assert (await host.before_agent_start("hello", "base", images)).system_prompt == ""
    assert images[0]["source"]["data"] == "original"
    await host.close()
    fresh = ExtensionHost([], tmp_path, "next")
    assert (await fresh.before_agent_start("hello", "base", images)).system_prompt == "base"
    await fresh.close()


# 功能：工具钩子拒绝或抛错时底层工具不执行，卸载后不遗留调用钩子
# 设计：走真实 invoke_tool 并监听失败事件，用工具替身调用次数证明没有副作用
@pytest.mark.parametrize("raises", [False, True])
async def test_extension_tool_call_blocks_execution(tmp_path: Path, raises: bool) -> None:
    host = ExtensionHost([str(FIXTURE)], tmp_path, "blocked")
    registry = ToolRegistry()
    bus = EventBus()
    events = []

    # 记录实际工具管线发出的事件
    async def record(event):
        events.append(event)

    # 模拟扩展主动拒绝和扩展自身执行错误
    def block(event):
        if raises:
            raise ValueError("Extension broken")
        return {"block": True, "reason": "Not now"}

    host.api.on("tool_call", block)
    bus.subscribe(record)
    await host.load(registry, bus)
    tool = registry.get("extension_greeting")
    assert tool is not None
    tool.invoke = AsyncMock()
    try:
        result = await invoke_tool(registry, ToolCallBlock(
            id="blocked-call", name="extension_greeting", input={},
        ), bus, "blocked")
        assert result.is_error and result.error_type == "hook_denied"
        tool.invoke.assert_not_called()
        assert any(event.type == "tool.call_failed" for event in events)
    finally:
        await host.close()
    assert registry.before_tool_call is None
    assert registry.after_tool_call is None


# 功能：结果钩子可替换或删除图片且不修改调用者原始图片，非法结果不污染后续钩子
# 设计：复用真实宿主链式转换并故意返回无效图片，检验结果提交是完整的而非半更新
async def test_extension_tool_result_images(tmp_path: Path) -> None:
    host = ExtensionHost([], tmp_path, "images")
    original = ToolResult("original", images=[{
        "type": "image", "source": {"type": "base64", "data": "original"},
    }])
    call = ToolCallBlock(id="image", name="read", input={})
    host.api.on("tool_result", lambda event: {"content": "invalid", "images": ["bad"]})
    unchanged = await host.after_tool_call(call, original)
    assert unchanged == original
    host.api.on("tool_result", lambda event: {"images": []})
    cleared = await host.after_tool_call(call, original)
    assert cleared.images == []
    assert original.images and original.images[0]["source"]["data"] == "original"
    await host.close()


# 功能：工具抛出的错误进入结果钩子，扩展可返回替代结果而不重复执行工具
# 设计：在真实管线中分别注入普通异常与超时，检查单次执行和唯一终态事件
@pytest.mark.parametrize("error", [RuntimeError("broken"), TimeoutError()])
async def test_extension_handles_execution_exception(tmp_path: Path, error: Exception) -> None:
    host = ExtensionHost([str(FIXTURE)], tmp_path, "recover")
    registry = ToolRegistry()
    bus = EventBus()
    seen = []
    received = []

    # 记录钩子收到的失败内容并提供无需额外执行的替代结果
    def recover(event):
        received.append(event)
        return {"content": "Fallback result", "is_error": False, "images": []}

    # 收集最终事件以排除先失败后成功的双重发布
    async def record(event):
        seen.append(event.type)

    host.api.on("tool_result", recover)
    await host.load(registry, bus)
    bus.subscribe(record)
    tool = registry.get("extension_greeting")
    assert tool is not None
    tool.invoke = AsyncMock(side_effect=error)
    try:
        result = await invoke_tool(registry, ToolCallBlock(
            id="recover-call", name=tool.name, input={},
        ), bus, "recover")
        assert received[0]["is_error"]
        assert not result.is_error
        assert result.content.startswith("Fallback result")
        tool.invoke.assert_awaited_once()
        assert seen.count("tool.call_finished") == 1
        assert "tool.call_failed" not in seen
    finally:
        await host.close()


# 功能：扩展覆盖原工具且先加载的同名扩展优先，关闭后恢复原实现和目录缓存
# 设计：预热 schema 缓存后加载两个同名扩展，比较实际调用、模型声明与恢复后的原始字节
async def test_extension_override_and_restore(tmp_path: Path) -> None:
    class Original(BaseTool):
        name = "extension_greeting"
        description = "Original tool"
        input_schema = {"type": "object", "properties": {}}
        side_effect = ToolSideEffect.NONE

        # 返回原始实现的标记以确认卸载恢复
        async def invoke(self, params):
            return ToolResult("original")

    original = Original()
    registry = ToolRegistry()
    registry.register(original)
    before = registry.canonical_catalog_json()
    host = ExtensionHost([
        str(FIXTURE), str(FIXTURE.with_name("native_extension_shadow.py")),
    ], tmp_path, "override")
    await host.load(registry)
    replacement = registry.get(original.name)
    assert replacement is not None and replacement is not original
    assert (await replacement.invoke({})).content == "Hello from a Python extension"
    schema = registry.canonical_catalog_json()
    assert b"Return a local greeting" in schema
    assert b"Later extension" not in schema
    await host.close()
    assert registry.get(original.name) is original
    assert registry.canonical_catalog_json() == before
    assert not host.tool_disposers


# 功能：扩展选择工具同步修改 schema 和模型调用能力，卸载恢复原目录
# 设计：预热缓存后清空活动工具并尝试读取，再重新启用以验证不是仅隐藏展示
async def test_extension_active_tools_control(tmp_path: Path) -> None:
    class DeferredRead(ReadTool):
        name = "extra_read"
        deferred = True

    registry = ToolRegistry()
    registry.register(ReadTool(WorkspaceBoundary(tmp_path)))
    registry.register(DeferredRead(WorkspaceBoundary(tmp_path)))
    (tmp_path / "note.txt").write_text("note", encoding="utf-8")
    before = registry.canonical_catalog_json()
    host = ExtensionHost([], tmp_path, "active")
    await host.load(registry)
    assert {tool["name"] for tool in host.api.get_all_tools()} == {"read", "extra_read"}
    assert host.api.get_active_tools() == ["read"]
    host.api.set_active_tools([])
    assert registry.canonical_catalog_json() == b"[]"
    call = ToolCallBlock(id="read", name="read", input={"path": "note.txt"})
    blocked = await invoke_tool(registry, call, EventBus(), "active")
    assert blocked.is_error
    host.api.set_active_tools(["read"])
    assert registry.canonical_catalog_json() == before
    assert not (await invoke_tool(registry, call, EventBus(), "active")).is_error
    host.api.set_active_tools(["extra_read"])
    assert host.api.get_active_tools() == ["extra_read"]
    deferred = await invoke_tool(registry, ToolCallBlock(
        id="extra", name="extra_read", input={"path": "note.txt"},
    ), EventBus(), "active")
    assert not deferred.is_error
    await host.close()
    assert registry.canonical_catalog_json() == before
    with pytest.raises(RuntimeError, match="ended"):
        host.api.get_active_tools()


# 功能：扩展切换活动工具进入后续真实模型请求及持久快照，系统提示同步更新
# 设计：假模型先读取再回答，比较两次请求而不是只检查注册表的局部返回值
async def test_dynamic_tools_reach_provider_and_snapshot(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(FIXTURE.with_name("native_extension_select_tools.py"))]
    (tmp_path / "note.txt").write_text("A useful note", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-dynamic", "chat", "active", "Dynamic", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Read note.txt")
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=[
        LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(id="read-one", name="read", input={"path": "note.txt"}),
        ]),
        LlmResponse(stop_reason="end_turn", text="Read completed"),
    ])
    result = await AgentRunner(config, provider=provider, workspace_root=tmp_path).run_and_capture(
        "Read note.txt", session=session, store=store,
    )
    assert result.status == "success"
    first, second = provider.chat.call_args_list
    assert [schema["name"] for schema in first.kwargs["tool_schemas"]] == ["read"]
    assert second.kwargs["tool_schemas"] == []
    assert "Available tools:\n(none)" in second.kwargs["system"]
    assert "A useful note" in str(second.kwargs["messages"])
    snapshots = [event for event in store.read_session_events(session.id)
                 if event.type == "llm.request_prepared"]
    assert len(snapshots) == 2
    for snapshot, request in zip(snapshots, (first, second), strict=True):
        assert snapshot.payload["tool_schemas"] == request.kwargs["tool_schemas"]
        assert snapshot.payload["system"] == request.kwargs["system"]
        assert snapshot.payload["messages"] == request.kwargs["messages"]


# 功能：上下文钩子影响每次实际请求和快照，但不改写会话历史或重复积累临时内容
# 设计：真实 Runner 完成读取后再次请求，核对工具结果数量、请求快照和重开历史
async def test_context_hooks_reach_request_not_history(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(FIXTURE.with_name("native_extension_context.py"))]
    (tmp_path / "note.txt").write_text("Context test", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-context", "chat", "active", "Context", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Read note.txt")
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=[
        LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(id="read-context", name="read", input={"path": "note.txt"}),
        ]),
        LlmResponse(stop_reason="end_turn", text="Read completed"),
    ])
    result = await AgentRunner(config, provider=provider, workspace_root=tmp_path).run_and_capture(
        "Read note.txt", session=session, store=store,
    )
    assert result.status == "success"
    snapshots = [event for event in store.read_session_events(session.id)
                 if event.type == "llm.request_prepared"]
    assert len(snapshots) == len(provider.chat.call_args_list) == 2
    for index, (snapshot, request) in enumerate(zip(
        snapshots, provider.chat.call_args_list, strict=True,
    )):
        messages = request.kwargs["messages"]
        assert str(messages).count("Request-only context:") == 1
        assert f"Request-only context: {index}" in str(messages)
        assert snapshot.payload["messages"] == messages
    reopened = SessionStore(tmp_path / "sessions")
    assert "Request-only context:" not in str(reopened.read_messages(session.id))
    assert "Read completed" in str(reopened.read_messages(session.id))


# 功能：只有整批工具都要求终止时才停止续调，混合批次继续且终止不冒充最终成功
# 设计：真实 Runner 分别覆盖调用前终止、结果终止和混合批次，检查模型请求次数与状态
@pytest.mark.parametrize("paths, request_count", [
    (["blocked.txt"], 1), (["note.txt"], 1), (["blocked.txt", "continue.txt"], 2),
])
async def test_extension_terminate_batch(tmp_path: Path, paths: list[str], request_count: int) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(FIXTURE.with_name("native_extension_terminate.py"))]
    for path in ("note.txt", "continue.txt"):
        (tmp_path / path).write_text("note", encoding="utf-8")
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=[
        LlmResponse(stop_reason="tool_use", tool_calls=[
            ToolCallBlock(id=f"read-{index}", name="read", input={"path": path})
            for index, path in enumerate(paths)
        ]),
        LlmResponse(stop_reason="end_turn", text="Completed"),
    ])
    result = await AgentRunner(config, provider=provider, workspace_root=tmp_path).run_and_capture(
        "Read the notes",
    )
    assert provider.chat.await_count == request_count
    assert result.status == ("success" if request_count == 2 else "failed")
    if request_count == 1:
        assert result.reason == "incomplete"


# 功能：Python 扩展可注册 Provider、列出模型并通过会话回调切换模型。
# 设计：加载真实扩展 fixture，解析两个模型并执行其选择命令，验证密钥只在冻结绑定中出现。
async def test_extension_registers_provider_and_selects_model(tmp_path: Path) -> None:
    host = ExtensionHost([str(PROVIDER_FIXTURE)], tmp_path, "run-provider")
    await host.initialize()
    selected: list[tuple[str, str]] = []

    # 捕获扩展命令发起的会话级模型选择
    async def set_model(provider: str, model: str) -> None:
        selected.append((provider, model))

    host.api.model_setter = set_model
    catalog = host.api.get_registered_providers()
    resolved = host.api.providers["local-proxy"].resolve("model-a")

    assert catalog == [{
        "id": "local-proxy",
        "name": "Local Python Proxy",
        "models": ["model-a", "model-b"],
    }]
    assert resolved.route.model == "model-a"
    assert resolved.route.context_window == 64000
    assert resolved.route.supports_images is True
    assert resolved.credential == "local-test-key"
    assert resolved.request_headers == {
        "X-Python-Provider": "model-a",
        "X-Literal-Dollar": "$value",
    }
    assert resolved.receipt.credential_source == "extension"
    assert "local-test-key" not in resolved.receipt.model_dump_json()
    native_provider = create_provider_for_resolved_route(resolved)
    assert isinstance(native_provider, OpenAICompatibleProvider)
    assert native_provider._headers == resolved.request_headers
    assert await host.execute_command("use-provider", "model-b") == "provider selected"
    assert await host.api.set_model("catalog-route", "gpt-coder")
    assert selected == [("local-proxy", "model-b"), ("catalog-route", "gpt-coder")]
    await host.close()


# 功能：Python 扩展可只覆盖已有 Provider 的端点和请求头而无需复制模型目录。
# 设计：构造带凭据的冻结基础路由并叠加覆盖，核对模型能力与密钥保留且 Header 留在内存。
def test_extension_overrides_existing_provider_without_models() -> None:
    route = ProviderRoute(
        id="team-openai",
        provider="openai",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("https://api.openai.com/v1/chat/completions"),
        model="gpt-coder",
        credential_ref="file:openai",
        catalog_id="openai",
        supports_images=True,
    )
    base = ResolvedRoute(
        route=route,
        receipt=route.receipt("file"),
        credential="saved-secret",
        request_headers={"X-Existing": "kept"},
    )
    override = ExtensionProvider.from_config("openai", {
        "baseUrl": "https://gateway.example/v1",
        "headers": {"X-Corp": "python-extension"},
    })

    resolved = override.resolve(base=base)

    assert resolved.route.model == "gpt-coder"
    assert str(resolved.route.base_url).rstrip("/") == (
        "https://gateway.example/v1/chat/completions"
    )
    assert resolved.route.supports_images is True
    assert resolved.credential == "saved-secret"
    assert resolved.receipt.credential_source == "file"
    assert resolved.request_headers == {
        "X-Existing": "kept",
        "X-Corp": "python-extension",
    }


# 功能：扩展 Provider 的命令型配置值只取 stdout，不把失败命令伪装为凭据。
# 设计：替换 subprocess 边界分别返回密钥和非零退出码，避免测试依赖平台 Shell。
def test_extension_provider_resolves_command_values(monkeypatch) -> None:
    completed = __import__("subprocess").CompletedProcess(
        args="secret-helper", returncode=0, stdout="runtime-secret\n", stderr="",
    )
    runner = MagicMock(return_value=completed)
    monkeypatch.setattr("code_rook.core.agent_runtime.providers.subprocess.run", runner)
    provider = ExtensionProvider.from_config("command-provider", {
        "baseUrl": "http://127.0.0.1:8080/v1",
        "apiKey": "!secret-helper",
        "api": "openai-completions",
        "headers": {"X-Session": "!session-helper"},
        "models": [{"id": "local-model"}],
    })

    resolved = provider.resolve()

    assert resolved.credential == "runtime-secret"
    assert resolved.request_headers == {"X-Session": "runtime-secret"}
    assert runner.call_count == 2

    runner.return_value = __import__("subprocess").CompletedProcess(
        args="secret-helper", returncode=9, stdout="do-not-use", stderr="failed",
    )
    with pytest.raises(Exception, match="status 9"):
        provider.resolve()


# 功能：扩展 Provider 的模型选择由 SessionManager 持久化并出现在共享上下文。
# 设计：把真实扩展宿主绑定到会话后执行命令，重读 meta 并核对前端可见目录。
async def test_extension_provider_selection_persists_in_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: MagicMock(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat", "Provider session")
    host = ExtensionHost([str(PROVIDER_FIXTURE)], tmp_path, "")
    await host.initialize()
    manager._extension_hosts[session.id] = host
    manager._bind_extension_messages(session.id, host, RuntimeMode.ACT)

    await host.execute_command("use-provider", "model-b")

    persisted = store.read_meta(session.id)
    context = manager.context_info(session.id)
    assert (persisted.route_id, persisted.model) == ("local-proxy", "model-b")
    assert host.api.get_model() == {
        "provider": "local-proxy",
        "id": "model-b",
        "thinking": "off",
    }
    assert context["extension_providers"][0]["models"] == ["model-a", "model-b"]
    await host.close()


# 功能：Provider 请求钩子的变换进入实际调用和 Request Snapshot，响应钩子观察统一结果。
# 设计：执行真实 Python AgentLoop，比较 Provider 入参、Ledger 快照与扩展落盘的响应正文。
async def test_extension_provider_hooks_wrap_authoritative_request(tmp_path: Path) -> None:
    config = CodeRookConfig()
    config.agent.extension_paths = [str(PROVIDER_HOOKS_FIXTURE)]
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn", text="Hooked response",
    ))
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-provider-hooks", "chat", "active", "Hooks", "now", "now")
    store.write_meta(session)
    store.append_message(session.id, "user", "Use provider hooks")

    outcome = await AgentRunner(
        config, provider=provider, workspace_root=tmp_path,
    ).run_and_capture("Use provider hooks", session=session, store=store)

    assert outcome.status == "success"
    request = provider.chat.call_args.kwargs
    assert request["system"].endswith("Extension provider hook active.")
    snapshot = next(
        event for event in store.read_session_events(session.id)
        if event.type == "llm.request_prepared"
    )
    assert snapshot.payload["system"] == request["system"]
    observed = __import__("json").loads(
        (tmp_path / "provider-response.json").read_text(encoding="utf-8")
    )
    assert observed["text"] == "Hooked response"

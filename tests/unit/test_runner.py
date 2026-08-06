from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_rook.core.authority import AuthoritySnapshot, RuntimeMode
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.route_registry import ResolvedRoute
from code_rook.core.llm.routes import get_route_preset
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runner import AgentRunner
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore

# --- mock provider -----------------------------------------------------------


class _EndTurnProvider:
    """Immediately returns end_turn; no API calls made."""

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        return LlmResponse(stop_reason="end_turn", text="done")


class _LoopingProvider:
    """Always returns tool_use with an unknown tool to exhaust max_steps."""

    def __init__(self) -> None:
        self._call = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self._call += 1
        tc = ToolCallBlock(id=f"t{self._call}", name="unknown_tool", input={})
        return LlmResponse(stop_reason="tool_use", tool_calls=[tc])


class _CapturingProvider:
    # 初始化捕获型 provider，保存固定响应
    def __init__(self, response: LlmResponse) -> None:
        self.response = response
        self.messages: list[dict[str, object]] = []
        self.tool_schemas: list[dict[str, object]] = []
        self.system: str | None = None

    # 捕获本次 LLM 调用的 messages 和 system prompt
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.messages = [dict(m) for m in messages]
        self.tool_schemas = [dict(schema) for schema in tool_schemas]
        self.system = system
        return self.response


class _ForcedPlanWriteProvider:
    # 初始化强制越权 provider 并记录首轮可见工具集合
    def __init__(self) -> None:
        self.calls = 0
        self.first_tool_names: set[str] = set()
        self.first_file_actions: set[str] = set()
        self.system = ""

    # 首轮故意请求 edit_file，次轮返回计划，验证 Core 不信任模型自律
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls += 1
        if self.calls == 1:
            self.first_tool_names = {str(schema["name"]) for schema in tool_schemas}
            file_schema = next(
                schema for schema in tool_schemas if schema["name"] == "File"
            )
            input_schema = file_schema["input_schema"]
            assert isinstance(input_schema, dict)
            variants = input_schema["oneOf"]
            assert isinstance(variants, list)
            self.first_file_actions = {
                str(variant["properties"]["action"]["enum"][0])
                for variant in variants
            }
            self.system = system or ""
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="write-in-plan",
                        name="edit_file",
                        input={
                            "path": "target.txt",
                            "old_text": "original",
                            "new_text": "changed",
                        },
                    )
                ],
            )
        return LlmResponse(
            stop_reason="end_turn",
            text="1. Inspect target.txt\n2. Apply the approved change\n3. Run tests",
        )


# --- helpers -----------------------------------------------------------------


def _config(max_steps: int = 5) -> CodeRookConfig:
    cfg = CodeRookConfig()
    cfg.agent.max_steps = max_steps
    return cfg


async def _run(
    goal: str = "test goal",
    *,
    provider: object | None = None,
    config: CodeRookConfig | None = None,
    tmp_path: Path,
) -> list[BaseModel]:
    collected: list[BaseModel] = []

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    cfg = config or _config()
    runner = AgentRunner(
        cfg,
        provider=provider or _EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[_collect],
        runs_dir=tmp_path,
    )
    await runner.run(goal)
    return collected


# --- tests -------------------------------------------------------------------


# 功能：验证 run 开始时发布携带正确 goal 的 run.started 事件
# 设计：用 extra_handlers 收集事件，而非从 events.jsonl 读取，避免文件 I/O 耦合；聚焦 runner 层的事件发布职责
async def test_run_started_event_published(tmp_path: Path) -> None:
    events = await _run(goal="my goal", tmp_path=tmp_path)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "run.started" in types
    started = next(e for e in events if e.type == "run.started")  # type: ignore[attr-defined]
    assert started.goal == "my goal"  # type: ignore[attr-defined]


# 功能：验证成功完成时发布 status=success 的 run.finished 事件
# 设计：EndTurnProvider 触发最短成功路径，聚焦 runner 层对任何终止路径都能保证发布 finished 事件
async def test_run_finished_event_published_on_success(tmp_path: Path) -> None:
    events = await _run(tmp_path=tmp_path)
    finished = next(
        (e for e in events if e.type == "run.finished"), None  # type: ignore[attr-defined]
    )
    assert finished is not None
    assert finished.status == "success"  # type: ignore[attr-defined]


# 功能：验证步数耗尽时 run.finished 携带 failed 状态和正确的失败原因
# 设计：LoopingProvider + max_steps=2 触发失败路径，确认 runner 在失败终止路径同样发布 finished 事件
async def test_run_finished_event_published_on_max_steps(tmp_path: Path) -> None:
    events = await _run(
        provider=_LoopingProvider(),
        config=_config(max_steps=2),
        tmp_path=tmp_path,
    )
    finished = next(e for e in events if e.type == "run.finished")  # type: ignore[attr-defined]
    assert finished.status == "failed"  # type: ignore[attr-defined]
    assert finished.reason == "exceeded_max_steps"  # type: ignore[attr-defined]


# 功能：验证 events.jsonl 第一行为 run.started、最后一行为 run.finished
# 设计：从 tmp_path 递归查找 events.jsonl 并按行解析，因为 events.jsonl 是 S1 的核心产物，首尾事件是完整性的最低要求
async def test_events_jsonl_created_with_started_and_finished(tmp_path: Path) -> None:
    await _run(tmp_path=tmp_path)
    jsonl_files = list(tmp_path.rglob("events.jsonl"))
    assert len(jsonl_files) == 1
    lines = [json.loads(ln) for ln in jsonl_files[0].read_text().splitlines() if ln]
    event_types = [e["type"] for e in lines]
    assert event_types[0] == "run.started"
    assert event_types[-1] == "run.finished"


# 功能：验证 runner 在 runs_dir 下创建以 run_id 命名的子目录并写入 events.jsonl
# 设计：检查 tmp_path 下只有一个子目录且该目录包含 events.jsonl，确认目录结构约定（runs/<run_id>/events.jsonl）
async def test_run_creates_run_subdirectory(tmp_path: Path) -> None:
    await _run(tmp_path=tmp_path)
    subdirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(subdirs) == 1
    assert (subdirs[0] / "events.jsonl").exists()


# 功能：验证通过 extra_handlers 注入的回调能收到所有事件
# 设计：注入第二个收集器，确认 extra_handlers 机制有效；这是测试代码注入 mock 观察器、生产代码接入 StdoutPrinter 的同一扩展点
async def test_extra_handlers_receive_events(tmp_path: Path) -> None:
    secondary: list[BaseModel] = []

    async def _second(e: BaseModel) -> None:
        secondary.append(e)

    cfg = _config()
    runner = AgentRunner(
        cfg,
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[_second],
        runs_dir=tmp_path,
    )
    await runner.run("goal")
    assert len(secondary) > 0


# 功能：验证 config.agent.max_steps 被正确传递给 AgentLoop，控制 LLM 调用次数上限
# 设计：用 LoopingProvider 的调用次数反推 max_steps 是否生效，不依赖内部状态检查，从行为角度验证配置传递
async def test_config_max_steps_passed_to_loop(tmp_path: Path) -> None:
    provider = _LoopingProvider()
    await _run(provider=provider, config=_config(max_steps=3), tmp_path=tmp_path)
    assert provider._call == 3


# 功能：验证 run.started 和 run.finished 事件使用相同且非空的 run_id
# 设计：同时检查两个事件的 run_id 字段，确认 runner 在整个 run 生命周期使用同一个 run_id
async def test_run_id_embedded_in_started_event(tmp_path: Path) -> None:
    events = await _run(tmp_path=tmp_path)
    started = next(e for e in events if e.type == "run.started")  # type: ignore[attr-defined]
    finished = next(e for e in events if e.type == "run.finished")  # type: ignore[attr-defined]
    assert started.run_id == finished.run_id  # type: ignore[attr-defined]
    assert len(started.run_id) > 0  # type: ignore[attr-defined]


# 功能：验证注入外部 EventBus 时，runner 使用该 bus 而不自建，外部订阅者能收到所有事件
# 设计：显式传入 EventBus 实例并订阅收集器，确认 runner 不再内部新建 bus（否则外部订阅者收不到事件）；
#       这是 CoreApp 注入全局 bus 的核心行为，单元测试级别验证可避免集成测试的守护进程依赖
async def test_injected_bus_receives_events(tmp_path: Path) -> None:
    from code_rook.core.events.bus import EventBus

    external_bus = EventBus()
    collected: list[object] = []

    async def collect(e: object) -> None:
        collected.append(e)

    external_bus.subscribe(collect)

    runner = AgentRunner(
        _config(),
        bus=external_bus,
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        runs_dir=tmp_path,
    )
    await runner.run("goal")

    types = [e.type for e in collected]  # type: ignore[attr-defined]
    assert "run.started" in types
    assert "run.finished" in types


# 功能：验证 session run 会从 thread.jsonl 预填 messages，并把 notes 注入 system prompt
# 设计：用 CapturingProvider 截获 LLM 入参，不触发真实 API；同时断言 run 目录写到 session/runs 下
async def test_session_history_and_notes_injected(tmp_path: Path) -> None:
    from code_rook.core.session.model import Session
    from code_rook.core.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess-1",
        mode="chat",
        status="active",
        title="",
        created_at="t",
        updated_at="t",
    )
    store.write_meta(session)
    store.append_message("sess-1", "user", "remember python")
    store.append_note("sess-1", "Python 3.12", "run-old")

    provider = _CapturingProvider(LlmResponse(stop_reason="end_turn", text="done"))
    runner = AgentRunner(
        _config(),
        provider=provider,
        runs_dir=tmp_path / "runs",
        workspace_root=tmp_path,
    )

    await runner.run_and_capture("remember python", run_id="run-new", session=session, store=store)

    assert provider.messages == [{"role": "user", "content": "remember python"}]
    assert provider.system is not None
    assert "Python 3.12" in provider.system
    assert (store.runs_dir("sess-1") / "run-new" / "events.jsonl").exists()
    assert not (tmp_path / "runs" / "run-new").exists()


# 功能：验证不同领域的意图纠正都获得同一套通用语义框架与完整历史
# 设计：参数化软件、配置、账户三种作用域误判，防止系统契约针对单一关键词过拟合
@pytest.mark.parametrize(
    ("initial", "wrong_answer", "correction"),
    [
        (
            "我有哪些agent",
            "当前没有正在运行的 CodeRook 内部 agent。",
            "我说的是我电脑上安装的 AI agent。",
        ),
        (
            "我有哪些配置",
            "项目中有一个 .coderook 配置目录。",
            "我指的是当前用户的 Git 全局配置。",
        ),
        (
            "有哪些账户",
            "仓库贡献者包括 Alice 和 Bob。",
            "我说的是这台电脑上的 Windows 用户账户。",
        ),
    ],
)
async def test_general_intent_correction_contract_is_injected(
    tmp_path: Path,
    initial: str,
    wrong_answer: str,
    correction: str,
) -> None:
    from code_rook.core.session.model import Session
    from code_rook.core.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-1", "chat", "active", "", "t", "t")
    store.write_meta(session)
    store.append_message("sess-1", "user", initial)
    store.append_message("sess-1", "assistant", wrong_answer)
    store.append_message("sess-1", "user", correction)

    provider = _CapturingProvider(LlmResponse(stop_reason="end_turn", text="done"))
    runner = AgentRunner(
        _config(),
        provider=provider,
        runs_dir=tmp_path / "runs",
        workspace_root=tmp_path,
    )

    await runner.run_and_capture(
        correction,
        run_id="run-new",
        session=session,
        store=store,
    )

    assert [message["role"] for message in provider.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert provider.system is not None
    assert "objective, target, scope, requested operation" in provider.system
    assert "clarifications and corrections as higher-priority evidence" in provider.system
    assert "discard incompatible assumptions and reselect tools" in provider.system
    assert "not from surface word overlap" in provider.system
    assert "failed, denied, or unavailable check is unknown" in provider.system
    assert "avoid redundant probes" in provider.system
    assert "Always use concise English for internal analysis" in provider.system
    assert "## Response Language" in provider.system
    assert "## Response Language\nFinal answer only: Simplified Chinese." in provider.system
    assert "Do not emit progress narration before tool calls" in provider.system
    assert "call tasks with the create and update actions" in provider.system
    assert "Never use emoji" in provider.system
    assert "## Runtime Environment" in provider.system
    assert "## Available Extensions" in provider.system
    schemas = {str(schema["name"]): schema for schema in provider.tool_schemas}
    assert "skill" in schemas
    assert "shell command in the workspace" in str(schemas["Bash"]["description"])
    assert "scope is only CodeRook task records" in str(schemas["tasks"]["description"])


# 功能：验证 session run 中注册了 note_save，工具调用会写入 notes.md
# 设计：mock provider 第一步请求 note_save、第二步 end_turn，覆盖 runner→registry→tool invocation 的完整路径
async def test_session_registers_note_save_tool(tmp_path: Path) -> None:
    from code_rook.core.session.model import Session
    from code_rook.core.session.store import SessionStore

    class _NoteProvider:
        # 初始化调用计数器，用于返回两步响应
        def __init__(self) -> None:
            self.calls = 0

        # 第一步请求 note_save，第二步返回 end_turn
        async def chat(
            self,
            messages: list[dict[str, object]],
            tool_schemas: list[dict[str, object]],
            bus: EventBus,
            run_id: str,
            *,
            step: int = 0,
            system: str | None = None,
        ) -> LlmResponse:
            self.calls += 1
            if self.calls == 1:
                return LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(
                            id="note-1",
                            name="note_save",
                            input={"content": "Use Python 3.12"},
                        )
                    ],
                )
            return LlmResponse(stop_reason="end_turn", text="noted")

    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess-1",
        mode="chat",
        status="active",
        title="",
        created_at="t",
        updated_at="t",
    )
    store.append_message("sess-1", "user", "remember")

    runner = AgentRunner(
        _config(max_steps=3),
        provider=_NoteProvider(),
        runs_dir=tmp_path,
        workspace_root=tmp_path,
    )
    await runner.run_and_capture("remember", run_id="run-1", session=session, store=store)

    assert "Use Python 3.12" in store.read_notes("sess-1")
    messages = store.read_messages("sess-1")
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    raw_rows = [
        json.loads(line)
        for line in (store.session_dir("sess-1") / "thread.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    block_ids = [row["block_id"] for row in raw_rows if row.get("kind") == "block"]
    assert len(block_ids) == len(set(block_ids)) == 3


# 功能：取消运行中的 run 时为未执行的 tool_use 补合成结果，transcript 保持协议闭环
# 设计：bash 工具执行到一半被 cancel，断言 read_messages 含 assistant(tool_use) 与 Skipped tool_result，
# 且尾部已平衡不产生 thread_interrupted 归档，验证孤儿补偿在取消路径同样生效
async def test_cancelled_runner_fills_skipped_tool_results(tmp_path: Path) -> None:
    from code_rook.core.session.model import Session
    from code_rook.core.session.store import SessionStore

    class _BashProvider:
        async def chat(
            self,
            messages: list[dict[str, object]],
            tool_schemas: list[dict[str, object]],
            bus: EventBus,
            run_id: str,
            *,
            step: int = 0,
            system: str | None = None,
        ) -> LlmResponse:
            command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="bash-1",
                            name="Bash",
                            input={"action": "run", "command": command, "timeout": 120},
                    )
                ],
            )

    tool_started = asyncio.Event()
    bus = EventBus()

    async def collect(event: BaseModel) -> None:
        if getattr(event, "type", "") == "tool.call_started":
            tool_started.set()

    bus.subscribe(collect)
    store = SessionStore(tmp_path / "sessions")
    session = Session("sess-1", "chat", "active", "", "t", "t")
    store.write_meta(session)
    store.append_message("sess-1", "user", "run a command", run_id="run-1")
    runner = AgentRunner(
        _config(),
        bus=bus,
        provider=_BashProvider(),  # type: ignore[arg-type]
        runs_dir=tmp_path / "runs",
    )

    task = asyncio.create_task(
        runner.run_and_capture("run a command", run_id="run-1", session=session, store=store)
    )
    await asyncio.wait_for(tool_started.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 孤儿补偿后取消会补合成 Skipped 结果，transcript 保持协议闭环而非被截断
    messages = store.read_messages("sess-1")
    assert messages[0] == {"role": "user", "content": "run a command"}
    tool_use_blocks = [
        block
        for block in messages[1]["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    assert tool_use_blocks and tool_use_blocks[0]["id"] == "bash-1"
    tool_result_blocks = [
        block
        for block in messages[2]["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    assert tool_result_blocks and tool_result_blocks[0]["tool_use_id"] == "bash-1"
    assert tool_result_blocks[0]["is_error"] is True
    assert "Skipped" in str(tool_result_blocks[0]["content"])
    # 尾部已平衡，无需恢复归档
    assert not list(store.session_dir("sess-1").glob("thread_interrupted_run-1_*.jsonl"))


# 功能：daemon 级 registry 下 runner 结束不取消后台子任务（生命周期由 daemon 管理）
# 设计：旧行为是在 runner 末尾 cancel_descendants，改为 daemon 级后 runner 不再自动杀
async def test_runner_no_longer_cancels_background_descendants(
    tmp_path: Path,
) -> None:
    runner = AgentRunner(
        _config(),
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        runs_dir=tmp_path,
    )
    child_context = ExecutionContext("child", "background", 1)
    child_event = asyncio.Event()
    child_task = asyncio.create_task(child_event.wait())
    runner._task_registry.register(  # type: ignore[attr-defined]
        "child",
        child_task,  # type: ignore[arg-type]
        child_context,
        parent_run_id="root",
    )

    await runner.run_and_capture("finish", run_id="root")

    # daemon 级：runner 结束不取消后台任务
    assert not child_task.cancelled()
    assert not child_context.is_done()
    child_event.set()
    await child_task


# 功能：验证 Plan Mode 不向模型暴露写工具，且强制伪造写调用也不能修改工作区
# 设计：provider 无视 schema 主动请求 edit_file，检查文件不变、计划提示存在且 session authority 被恢复
async def test_plan_mode_enforces_read_only_registry_and_restores_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "target.txt"
    target.write_text("original", encoding="utf-8")
    store = SessionStore(tmp_path / "sessions")
    session = Session(
        id="sess-plan",
        mode="chat",
        status="active",
        title="plan",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
    )
    store.write_meta(session)
    store.append_message(session.id, "user", "Plan a safe change")
    permission_manager = PermissionManager()
    original_authority = AuthoritySnapshot(mode=RuntimeMode.ACT)
    permission_manager.set_authority_snapshot(session.id, original_authority)
    provider = _ForcedPlanWriteProvider()
    runner = AgentRunner(
        _config(),
        provider=provider,
        permission_manager=permission_manager,
        workspace_root=workspace,
        runs_dir=tmp_path / "runs",
    )

    outcome = await runner.run_and_capture(
        "Plan a safe change",
        run_id="run-plan",
        session=session,
        store=store,
        runtime_mode=RuntimeMode.PLAN,
    )

    assert outcome.status == "success"
    assert target.read_text(encoding="utf-8") == "original"
    assert "edit_file" not in provider.first_tool_names
    assert "write_file" not in provider.first_tool_names
    assert "bash" not in provider.first_tool_names
    assert {"File", "Git"} <= provider.first_tool_names
    assert "git_diff" not in provider.first_tool_names
    assert provider.first_file_actions == {
        "read",
        "list",
        "search_name",
        "search_content",
    }
    assert "## Plan Mode" in provider.system
    assert permission_manager.get_authority_snapshot(session.id) == original_authority


# 功能：验证 Runner 发布实际 route receipt 事件且事件序列化不包含密钥正文
# 设计：给注入 provider 的最短成功 run 同时传入 ResolvedRoute，收集事件并核对所有 receipt 字段
async def test_runner_publishes_redacted_route_receipt(tmp_path: Path) -> None:
    events: list[BaseModel] = []

    # 收集 Runner 发布的所有事件
    async def collect(event: BaseModel) -> None:
        events.append(event)

    route = get_route_preset("openai", model="gpt-route-test")
    resolved = ResolvedRoute(
        route=route,
        receipt=route.receipt("keyring"),
        credential="route-secret",
    )
    runner = AgentRunner(
        _config(),
        provider=_EndTurnProvider(),  # type: ignore[arg-type]
        extra_handlers=[collect],
        runs_dir=tmp_path,
    )

    outcome = await runner.run_and_capture("hello", resolved_route=resolved)

    selected = next(event for event in events if event.type == "llm.route_selected")  # type: ignore[attr-defined]
    assert outcome.status == "success"
    assert selected.route_id == route.id  # type: ignore[attr-defined]
    assert selected.wire_format == route.wire_format  # type: ignore[attr-defined]
    assert selected.base_url_origin == "https://api.openai.com"  # type: ignore[attr-defined]
    assert selected.credential_source == "keyring"  # type: ignore[attr-defined]
    assert "route-secret" not in selected.model_dump_json()

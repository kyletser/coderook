from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_rook.core.artifacts import ArtifactStore, ImageArtifactInput
from code_rook.core.authority import AuthoritySnapshot, RuntimeMode, WorkspaceTrust
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.context import ExecutionContext
from code_rook.core.editing import FileMutation, apply_file_transaction
from code_rook.core.events.bus import EventBus
from code_rook.core.goal import GoalService, GoalStore
from code_rook.core.hooks import HookManager
from code_rook.core.runner import RunOutcome
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.manager import (
    RUN_NOT_ACTIVE,
    SESSION_BUSY,
    SESSION_CLOSED,
    SESSION_NOT_FOUND,
    SESSION_NOT_RESUMABLE,
    SessionManager,
)
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.workspace import WorkspaceBoundary


class _Runner:
    # 原样返回测试注入的冻结 route，模拟生产 Runner 的 Turn 级选择入口
    async def resolve_turn_binding(
        self,
        *,
        resolved_route: object | None,
        runtime_mode: RuntimeMode,
        run_id: str,
    ) -> object | None:
        return resolved_route

    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


class _ImageRunner(_Runner):
    # 初始化图片捕获槽以验证像素只通过临时参数进入 runner
    def __init__(self) -> None:
        self.initial_images: list[dict[str, object]] = []

    # 捕获初始图片后复用普通 runner 的 transcript 行为
    async def run_and_capture(
        self,
        goal: str,
        *,
        initial_images: list[dict[str, object]] | None = None,
        **kwargs: object,
    ) -> RunOutcome:
        self.initial_images = initial_images or []
        return await super().run_and_capture(goal, **kwargs)  # type: ignore[arg-type]


class _GoalRunner(_Runner):
    # 初始化持久 Goal 上下文捕获槽
    def __init__(self) -> None:
        self.goal_context = ""

    # 捕获 Goal 系统上下文后复用成功 runner 行为
    async def run_and_capture(
        self,
        goal: str,
        *,
        persistent_goal_context: str = "",
        **kwargs: object,
    ) -> RunOutcome:
        self.goal_context = persistent_goal_context
        return await super().run_and_capture(goal, **kwargs)  # type: ignore[arg-type]


# 功能：验证 daemon 启动后自动删除超过保留期且从未使用的空会话
# 设计：同时构造过期空会话、过期已命名会话和新空会话，确认只剪枝无用项且同步 Runtime
async def test_session_bootstrap_prunes_only_stale_unused_empty_sessions(
    tmp_path: Path,
) -> None:
    class _Runtime:
        # 初始化被删除的 Runtime thread 记录
        def __init__(self) -> None:
            self.deleted: list[str] = []

        # 接受会话投影启动而不修改 fixture
        async def bootstrap_sessions(self, *_args: object) -> None:
            return None

        # 记录自动剪枝对应的 Runtime thread
        async def delete_session(self, session_id: str) -> None:
            self.deleted.append(session_id)

    store = SessionStore(tmp_path / "sessions")
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    recent = datetime.now(UTC).isoformat()
    stale = Session("sess-stale", "chat", "interrupted", "", old, old)
    named = Session("sess-named", "chat", "interrupted", "Keep me", old, old)
    fresh = Session("sess-fresh", "chat", "interrupted", "", recent, recent)
    for session in (stale, named, fresh):
        store.write_meta(session)
    runtime = _Runtime()
    manager = SessionManager(
        store,
        lambda: _Runner(),
        EventBus(),
        runtime_service=runtime,  # type: ignore[arg-type]
    )  # type: ignore[arg-type]

    sessions = await manager.list_sessions(include_closed=True)

    assert {session.id for session in sessions} == {"sess-named", "sess-fresh"}
    assert runtime.deleted == ["sess-stale"]
    assert not store.session_dir("sess-stale").exists()


# 功能：验证 session_start、message_submit、turn_start、session_stop 接入真实会话生命周期
# 设计：共享一个 HookManager 贯穿 create/send/close，使用内存回调核对稳定事件顺序
async def test_session_lifecycle_emits_hooks_v2(tmp_path: Path) -> None:
    hooks = HookManager()
    seen: list[str] = []

    # 构造为每个事件记录固定名称的异步回调
    def callback(event_name: str):
        # 将事件名称闭包成符合 HookCallback 协议的协程
        async def receive(_context: dict[str, object]) -> None:
            seen.append(event_name)

        return receive

    for event_name in ("session_start", "message_submit", "turn_start", "session_stop"):
        hooks.register(event_name, callback(event_name))  # type: ignore[arg-type]
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: _Runner(),
        EventBus(),
        hooks=hooks,
    )  # type: ignore[arg-type]

    session = await manager.create("chat")
    await manager.send_message(session.id, "hello")
    await manager.close(session.id)

    assert seen == ["session_start", "message_submit", "turn_start", "session_stop"]


# 功能：验证 create 会创建 active session、写入 meta 并发布 session.created 事件
# 设计：用真实 SessionStore + EventBus 收集事件，覆盖 manager 与 store/bus 的协作边界
async def test_create_session_writes_meta_and_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert store.read_meta(session.id).title == "title"
    assert [e.type for e in events] == ["session.created"]  # type: ignore[attr-defined]


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证图片附件只把 artifact 引用写入 transcript，并把 base64 像素临时交给 runner
# 设计：用内容寻址 PNG 和捕获 runner 串联 send_message，分别检查 durable 与 transient 两侧
async def test_send_message_delivers_image_artifact_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    boundary = WorkspaceBoundary(workspace)
    monkeypatch.setattr(WorkspaceBoundary, "current", classmethod(lambda cls: boundary))
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (2).to_bytes(4, "big") * 2
    artifact = await ArtifactStore(workspace / ".coderook" / "artifacts").put(
        png,
        media_type="image/png",
    )
    runner = _ImageRunner()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: runner, EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    await manager.send_message(
        session.id,
        "分析截图",
        attachments=[
            ImageArtifactInput(
                sha256=artifact.sha256,
                media_type="image/png",
                size=len(png),
                width=2,
                height=2,
            )
        ],
    )

    user_content = str(store.read_messages(session.id)[0]["content"])
    assert f"artifact:{artifact.sha256}" in user_content
    assert "base64" not in user_content
    assert runner.initial_images[0]["type"] == "image"


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 coderook run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert store.read_meta(session.id).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


# 功能：daemon 冷启动时保留未完成操作证据，并把运行中的会话标记为 interrupted
# 设计：预写未配对工具调用后重建 manager，证明模型投影安全裁剪但 append-only 账本不被破坏
async def test_rehydrate_marks_active_session_interrupted(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    session = Session(
        id="sess-recover",
        mode="chat",
        status="active",
        title="recover me",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        run_ids=["run-old"],
    )
    store.write_meta(session)
    store.append_message("sess-recover", "user", "before crash", run_id="run-old")
    SessionTranscriptSink(store, "sess-recover", "run-old").append_assistant(
        1,
        [{"type": "tool_use", "id": "orphan", "name": "bash", "input": {}}],
    )

    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    sessions = await manager.list_sessions()

    assert [item.id for item in sessions] == ["sess-recover"]
    assert sessions[0].status == "interrupted"
    assert store.read_meta("sess-recover").status == "interrupted"
    assert store.read_messages("sess-recover") == [
        {"role": "user", "content": "before crash"}
    ]
    assert [call.tool_use_id for call in store.find_incomplete_tool_calls("sess-recover")] == [
        "orphan"
    ]
    assert not list(store.session_dir("sess-recover").glob("thread_interrupted_*.jsonl"))


# 功能：恢复中断会话时区分只读可重跑与修改状态未知，并发布可操作恢复卡
# 设计：分别构造 read_file 和 Bash 未配对调用，核对 safe_to_resume 与 interruption_kind 的保守分类
@pytest.mark.parametrize(
    ("tool_name", "safe_to_resume", "interruption_kind"),
    [
        ("read_file", True, "read_tool_interrupted"),
        ("Bash", False, "tool_state_unknown"),
    ],
)
async def test_resume_classifies_incomplete_tool_recovery(
    tmp_path: Path,
    tool_name: str,
    safe_to_resume: bool,
    interruption_kind: str,
) -> None:
    store = SessionStore(tmp_path / tool_name)
    session = Session(
        id=f"sess-{tool_name.lower()}",
        mode="chat",
        status="active",
        title="recover",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-02T00:00:00Z",
        run_ids=["run-old"],
    )
    store.write_meta(session)
    store.append_message(session.id, "user", "continue", run_id="run-old")
    SessionTranscriptSink(store, session.id, "run-old").append_assistant(
        1,
        [{"type": "tool_use", "id": "pending", "name": tool_name, "input": {}}],
    )
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集恢复事件以验证用户可见分类，不读取内部集合实现细节
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    await manager.resume(session.id)

    recovery = next(
        event for event in events if getattr(event, "type", "") == "recovery.available"
    )
    assert getattr(recovery, "safe_to_resume") is safe_to_resume
    assert getattr(recovery, "interruption_kind") == interruption_kind


# 功能：closed chat 可显式 resume，并继续沿用原 thread
# 设计：跨 manager 实例恢复后发送新消息，验证历史没有创建新 session 或丢失
async def test_resume_closed_chat_continues_existing_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    first = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await first.create("chat", "persistent")
    await first.send_message(session.id, "first")
    await first.close(session.id)

    second = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    resumed = await second.resume(session.id)
    await second.send_message(session.id, "second")

    assert resumed.id == session.id
    assert [message["content"] for message in store.read_messages(session.id) if message["role"] == "user"] == [
        "first",
        "second",
    ]


# 功能：one-shot session 不可伪装成可继续聊天的 session
# 设计：恢复接口只接受 chat，避免一次性任务状态机被重复执行
async def test_resume_rejects_one_shot_session(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    with pytest.raises(HandlerError) as exc:
        await manager.resume(session.id)
    assert exc.value.code == SESSION_NOT_RESUMABLE


# 功能：验证 daemon 只加载当前 workspace 的会话并拒绝恢复其他仓库的 session
# 设计：在同一全局 SessionStore 写入两个 workspace 的 meta，再切换 cwd 构建 manager 并检查隔离边界
async def test_session_manager_scopes_sessions_to_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_a = tmp_path / "repo-a"
    workspace_b = tmp_path / "repo-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    store = SessionStore(tmp_path / "sessions")
    for session_id, workspace in (
        ("sess-aaaaaaaaaaaa", workspace_a),
        ("sess-bbbbbbbbbbbb", workspace_b),
    ):
        store.write_meta(
            Session(
                id=session_id,
                mode="chat",
                status="waiting_for_input",
                title=session_id,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:00Z",
                workspace=str(workspace),
            )
        )
    monkeypatch.chdir(workspace_a)

    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    assert [session.id for session in await manager.list_sessions()] == [
        "sess-aaaaaaaaaaaa"
    ]
    with pytest.raises(HandlerError) as exc:
        await manager.resume("sess-bbbbbbbbbbbb")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：发送消息期间把 meta 持久化为 active，供崩溃恢复识别中断 run
# 设计：阻塞 runner，在完成前读取磁盘状态，再放行并检查最终 waiting 状态
async def test_send_message_persists_active_state_during_run(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingRunner(_Runner):
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            started.set()
            await release.wait()
            return RunOutcome(status="success", result="done", reason=None)

    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _BlockingRunner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    task = asyncio.create_task(manager.send_message(session.id, "work"))

    await started.wait()
    assert store.read_meta(session.id).status == "active"
    release.set()
    await task
    assert store.read_meta(session.id).status == "waiting_for_input"


# 功能：验证 SessionManager 将 active Goal 注入 runner，且单轮成功后仍保留长期目标
# 设计：使用捕获系统上下文的轻量 runner 执行真实 manager 生命周期，同时重读 Goal 文件排除伪完成证据
async def test_session_manager_executes_and_preserves_active_goal(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    goals = GoalService(GoalStore(tmp_path / "goals"))
    runner = _GoalRunner()
    manager = SessionManager(
        store,
        lambda: runner,
        EventBus(),
        goal_service=goals,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "ship durable goal",
        session_id=session.id,
        completion_criteria=["tests pass"],
    )

    run_id = await manager.send_message(session.id, goal.objective)
    progressed = goals.get(goal.id)

    assert "Objective: ship durable goal" in runner.goal_context
    assert "Completion criteria:\n- tests pass" in runner.goal_context
    assert "call update_goal with status=completed" in runner.goal_context
    assert progressed.status == "active"
    assert progressed.linked_run_ids == [run_id]
    assert progressed.completion_evidence == []


# 功能：验证 SessionManager 根据 typed 决策自动启动下一轮并在硬轮次上限暂停
# 设计：只发送初始消息，以 Turn 总上限二等待唯一续跑，排除客户端手动驱动的伪 Loop
async def test_session_manager_publishes_bounded_goal_decisions(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    # 收集 EventBus 对象以核对 typed Goal 决策事件
    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path / "sessions")
    goals = GoalService(GoalStore(tmp_path / "goals"))
    runner = _GoalRunner()
    manager = SessionManager(
        store,
        lambda: runner,
        bus,
        goal_service=goals,
        authority_provider=lambda _sid: AuthoritySnapshot(),
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "bounded delivery",
        session_id=session.id,
        auto_continue=True,
        max_auto_turns=2,
        completion_criteria=["tests pass"],
    )

    await manager.send_message(session.id, "initial work")
    for _attempt in range(100):
        if goals.get(goal.id).status == "paused":
            break
        await asyncio.sleep(0.01)

    decisions = [
        event for event in events if getattr(event, "type", "") == "goal.continue_decision"
    ]
    assert [getattr(event, "reason", "") for event in decisions] == [
        "ready_for_bounded_continuation",
        "max_auto_turns_reached",
    ]
    assert len(store.read_meta(session.id).run_ids) == 2
    assert goals.get(goal.id).status == "paused"
    assert goals.get(goal.id).paused_needs_confirmation is True
    await manager.cancel_all()


# 功能：验证 Goal 剩余墙钟会作为单个 Turn 的硬 deadline 并在超时后进入确认暂停
# 设计：把服务剩余额缩短到毫秒级并运行可取消 stub，断言协程已取消且不会继续自动调度
async def test_session_manager_enforces_goal_wall_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SlowGoalRunner:
        # 初始化取消观测标志
        def __init__(self) -> None:
            self.cancelled = False

        # 阻塞到 deadline 取消并在 finally 中记录清理已发生
        async def run_and_capture(self, *_args: object, **_kwargs: object) -> RunOutcome:
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled = True

    store = SessionStore(tmp_path / "sessions")
    goals = GoalService(GoalStore(tmp_path / "goals"))
    runner = _SlowGoalRunner()
    manager = SessionManager(
        store,
        lambda: runner,
        EventBus(),
        goal_service=goals,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "stop at wall deadline",
        session_id=session.id,
        auto_continue=True,
        completion_criteria=["verified"],
    )
    monkeypatch.setattr(goals, "remaining_wall_seconds", lambda _goal_id: 0.01)

    await manager.send_message(session.id, "start bounded work")

    persisted = goals.get(goal.id)
    assert runner.cancelled is True
    assert persisted.status == "paused"
    assert persisted.paused_reason == "max_wall_seconds_reached"
    assert persisted.paused_needs_confirmation is True
    assert len(store.read_meta(session.id).run_ids) == 1
    await manager.cancel_all()


# 功能：验证自动 Goal 只对明确 transport/stream 故障续跑，普通 llm_error 必须阻塞
# 设计：经 SessionManager 的生产分类入口完成两个隔离 run，比较 blocked 与 bounded retry 决策
async def test_auto_goal_retries_only_explicit_transient_failures(tmp_path: Path) -> None:
    goals = GoalService(GoalStore(tmp_path / "goals"))
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: _GoalRunner(),
        EventBus(),
        goal_service=goals,
    )  # type: ignore[arg-type]
    generic = goals.create(
        "do not retry auth or config failures",
        session_id="sess-generic",
        auto_continue=True,
        completion_criteria=["verified"],
    )
    goals.start_run(generic.id, "run-generic")

    generic_decision = await manager._finish_goal_run(
        generic.id,
        "run-generic",
        succeeded=False,
        reason="llm_error",
    )

    assert generic_decision is not None
    assert generic_decision.should_continue is False
    assert goals.get(generic.id).status == "blocked"

    transport = goals.create(
        "retry a dropped transport",
        session_id="sess-transport",
        auto_continue=True,
        completion_criteria=["verified"],
    )
    goals.start_run(transport.id, "run-transport")
    transport_decision = await manager._finish_goal_run(
        transport.id,
        "run-transport",
        succeeded=False,
        reason="transport_error",
    )

    assert transport_decision is not None
    assert transport_decision.should_continue is True
    assert goals.get(transport.id).status == "active"

    reserved = goals.create(
        "retry after concurrent lease settles",
        session_id="sess-reserved",
        auto_continue=True,
        completion_criteria=["verified"],
    )
    goals.start_run(reserved.id, "run-reserved")
    reserved_decision = await manager._finish_goal_run(
        reserved.id,
        "run-reserved",
        succeeded=False,
        reason="token_budget_reserved",
    )

    assert reserved_decision is not None
    assert reserved_decision.should_continue is True
    assert goals.get(reserved.id).status == "active"


# 功能：验证 Goal run 在 session ledger 首次写入前已原子登记 current_run_id
# 设计：包装真实 SessionStore.append_message 并从 GoalStore 回读，覆盖 user 与 runner 消息的持久顺序
async def test_goal_run_is_reserved_before_session_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    goals = GoalService(GoalStore(tmp_path / "goals"))
    manager = SessionManager(
        store,
        lambda: _GoalRunner(),
        EventBus(),
        goal_service=goals,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "persist in order",
        session_id=session.id,
        completion_criteria=["done"],
    )
    observed: list[str | None] = []
    original_append = store.append_message

    # 每次 ledger 写入前从独立文件真值确认 Goal 已持有本轮预留
    def checked_append(*args: object, **kwargs: object) -> None:
        observed.append(goals.get(goal.id).current_run_id)
        original_append(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "append_message", checked_append)
    run_id = await manager.send_message(session.id, "start")

    assert observed
    assert all(item == run_id for item in observed)
    assert goals.get(goal.id).current_run_id is None


# 功能：验证 Goal 预留后的 session 持久化失败会取消未启动 runner 并原子终结预留
# 设计：让首条 ledger append 抛出磁盘错误，核对 Goal blocked、session interrupted 且无 active runner
async def test_goal_run_reservation_rolls_back_when_session_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SessionStore(tmp_path / "sessions")
    goals = GoalService(GoalStore(tmp_path / "goals"))
    runner = _GoalRunner()
    manager = SessionManager(
        store,
        lambda: runner,
        EventBus(),
        goal_service=goals,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "fail consistently",
        session_id=session.id,
        completion_criteria=["done"],
    )

    # 模拟目标磁盘在首次 Turn ledger 写入时失败
    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "append_message", fail_append)
    with pytest.raises(OSError, match="disk full"):
        await manager.send_message(session.id, "start")

    persisted = goals.get(goal.id)
    assert persisted.status == "blocked"
    assert persisted.current_run_id is None
    assert persisted.timeline[-1].event == "goal.run_aborted"
    assert manager.active_run_id(session.id) is None
    assert store.read_meta(session.id).status == "interrupted"


# 功能：验证 runtime Turn 创建失败与 ledger 失败使用同一 Goal 回滚终态
# 设计：让 start_turn 在异步持久边界抛错，核对未启动 runner、Goal blocked 和 session interrupted
async def test_goal_run_reservation_rolls_back_when_runtime_start_fails(
    tmp_path: Path,
) -> None:
    class _FailingRuntime:
        # 接受 session bootstrap 以让失败精确发生在目标 Turn 创建
        async def bootstrap_sessions(self, *_args: object) -> None:
            return None

        # 接受 session 创建投影
        async def sync_session(self, *_args: object) -> None:
            return None

        # 模拟 runtime 持久存储在创建 Turn 时失败
        async def start_turn(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("runtime disk full")

        # 模拟不存在的部分 Turn 无法二次终结
        async def finish_turn(self, *_args: object, **_kwargs: object) -> None:
            raise LookupError("turn was not created")

    class _CountingRunner(_GoalRunner):
        # 初始化执行计数以证明 runtime 失败不会越过启动屏障
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        # 记录任何不应发生的 runner 执行
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            self.calls += 1
            return await super().run_and_capture(*args, **kwargs)

    goals = GoalService(GoalStore(tmp_path / "goals"))
    runner = _CountingRunner()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: runner,
        EventBus(),
        runtime_service=_FailingRuntime(),  # type: ignore[arg-type]
        goal_service=goals,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "runtime failure",
        session_id=session.id,
        completion_criteria=["done"],
    )

    with pytest.raises(OSError, match="runtime disk full"):
        await manager.send_message(session.id, "start")

    persisted = goals.get(goal.id)
    assert persisted.status == "blocked"
    assert persisted.current_run_id is None
    assert store.read_meta(session.id).status == "interrupted"
    assert runner.calls == 0


# 功能：验证 pause 在 runtime 启动窗口内仍能找到内存 runner 并留下单一暂停终态
# 设计：阻塞 runtime.start_turn 后先 pause 再 cancel，确保启动屏障让 runner 从未执行且 Goal 无悬挂 run
async def test_goal_pause_during_turn_preparation_cancels_barrier_runner(
    tmp_path: Path,
) -> None:
    class _BlockingRuntime:
        # 初始化 runtime 启动阻塞点与终态记录
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.finished = False

        # 接受 session bootstrap 而不产生额外状态
        async def bootstrap_sessions(self, *_args: object) -> None:
            return None

        # 接受 session 同步而不产生额外状态
        async def sync_session(self, *_args: object) -> None:
            return None

        # 在 runtime Turn 创建处阻塞以暴露 pause 竞态窗口
        async def start_turn(self, *_args: object, **_kwargs: object) -> None:
            self.started.set()
            await self.release.wait()

        # 记录 SessionManager 已将取消 Turn 转为显式终态
        async def finish_turn(self, *_args: object, **_kwargs: object) -> None:
            self.finished = True

    class _NeverRunner(_GoalRunner):
        # 初始化执行计数以证明启动屏障前的取消没有进入 runner
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        # 若被真正启动则增加计数后复用普通行为
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            self.calls += 1
            return await super().run_and_capture(*args, **kwargs)

    runtime = _BlockingRuntime()
    runner = _NeverRunner()
    goals = GoalService(GoalStore(tmp_path / "goals"))
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: runner,
        EventBus(),
        runtime_service=runtime,  # type: ignore[arg-type]
        goal_service=goals,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    goal = goals.create(
        "pause preparation",
        session_id=session.id,
        completion_criteria=["done"],
    )
    send_task = asyncio.create_task(manager.send_message(session.id, "start"))
    await runtime.started.wait()
    active_run_id = manager.active_run_id(session.id)
    assert active_run_id is not None

    goals.pause(goal.id)
    cancel_task = asyncio.create_task(manager.cancel_run(active_run_id))
    runtime.release.set()
    await cancel_task
    await send_task

    persisted = goals.get(goal.id)
    assert persisted.status == "paused"
    assert persisted.current_run_id is None
    assert runner.calls == 0
    assert runtime.finished is True


# 功能：验证工作区变更会等待并发 Turn 排空，并在持锁期间阻止其他 session 启动新 Turn
# 设计：先让两个 session runner 同时阻塞，再申请 mutation，随后检查第三个 Turn 只能在变更释放后进入
async def test_workspace_mutation_guard_coordinates_all_sessions(tmp_path: Path) -> None:
    class _GuardRunner(_Runner):
        # 初始化独立进入与释放事件供三个 session 精确编排
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        # 标记进入 runner 后等待测试释放
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            self.started.set()
            await self.release.wait()
            return await super().run_and_capture(*args, **kwargs)

    runners = [_GuardRunner(), _GuardRunner(), _GuardRunner()]
    runner_iter = iter(runners)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: next(runner_iter),
        EventBus(),
    )  # type: ignore[arg-type]
    sessions = [await manager.create("chat") for _ in range(3)]
    first = asyncio.create_task(manager.send_message(sessions[0].id, "one"))
    second = asyncio.create_task(manager.send_message(sessions[1].id, "two"))
    await asyncio.gather(runners[0].started.wait(), runners[1].started.wait())

    mutation_entered = asyncio.Event()
    mutation_release = asyncio.Event()

    # 进入独占窗口后保持占用，供第三个 Turn 验证不能穿透
    async def mutate_workspace() -> None:
        async with manager.workspace_mutation():
            mutation_entered.set()
            await mutation_release.wait()

    mutation = asyncio.create_task(mutate_workspace())
    await asyncio.sleep(0)
    assert not mutation_entered.is_set()
    runners[0].release.set()
    runners[1].release.set()
    await asyncio.gather(first, second)
    await mutation_entered.wait()

    third = asyncio.create_task(manager.send_message(sessions[2].id, "three"))
    await asyncio.sleep(0)
    assert not runners[2].started.is_set()
    mutation_release.set()
    await mutation
    await runners[2].started.wait()
    runners[2].release.set()
    await third


# 功能：验证 slash skill 的正文仅在 session workspace trust 为 trusted 时进入 runner
# 设计：同一 manager 先以 untrusted 调用并断言拒绝，再切换 authority 快照后执行本地 skill
async def test_session_skill_execution_consumes_workspace_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".coderook" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "local.md").write_text(
        "---\nname: local\ndescription: local\n---\ntrusted skill $ARGUMENTS\n",
        encoding="utf-8",
    )
    boundary = WorkspaceBoundary(workspace)
    monkeypatch.setattr(WorkspaceBoundary, "current", classmethod(lambda cls: boundary))
    current_trust = WorkspaceTrust.UNTRUSTED

    class _SkillRunner(_Runner):
        # 初始化捕获槽以证明 skill 正文确实进入 runner
        def __init__(self) -> None:
            self.seen_goal = ""

        # 捕获展开后的 skill 目标并复用普通成功 runner
        async def run_and_capture(self, goal: str, **kwargs: object) -> RunOutcome:
            self.seen_goal = goal
            return await super().run_and_capture(goal, **kwargs)  # type: ignore[arg-type]

    runner = _SkillRunner()
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: runner,
        EventBus(),
        authority_provider=lambda _sid: AuthoritySnapshot(
            workspace_trust=current_trust
        ),
    )  # type: ignore[arg-type]
    session = await manager.create("chat")

    with pytest.raises(HandlerError, match="not trusted"):
        await manager.send_message(session.id, "/local target.py")

    current_trust = WorkspaceTrust.TRUSTED
    await manager.send_message(session.id, "/local target.py")
    assert runner.seen_goal == "trusted skill target.py"


async def test_cancel_run_interrupts_runner_and_releases_session_lock(tmp_path: Path) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)

    class _BlockingRunner(_Runner):
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runners = iter([_BlockingRunner(), _Runner()])
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: next(runners), bus)  # type: ignore[arg-type]
    session = await manager.create("chat")
    send_task = asyncio.create_task(manager.send_message(session.id, "long task"))
    await started.wait()
    run_id = store.read_meta(session.id).run_ids[-1]

    cancelled_session = await manager.cancel_run(run_id)
    returned_run_id = await send_task

    assert cancelled_session == session.id
    assert returned_run_id == run_id
    assert cancelled.is_set()
    assert store.read_meta(session.id).status == "interrupted"
    assert any(getattr(event, "type", "") == "session.interrupted" for event in events)

    await manager.send_message(session.id, "continue")
    assert store.read_meta(session.id).status == "waiting_for_input"


# 功能：用户显式取消父 run 时同步取消其后台子 Agent，但不影响无关任务
# 设计：给 SessionManager 注入 daemon registry，注册直属 child 与 unrelated 后取消真实阻塞 run
async def test_cancel_run_cascades_to_background_descendants(tmp_path: Path) -> None:
    started = asyncio.Event()

    class _BlockingRunner(_Runner):
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    registry = BackgroundTaskRegistry()
    store = SessionStore(tmp_path)
    manager = SessionManager(
        store,
        lambda: _BlockingRunner(),
        EventBus(),
        subagent_registry=registry,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")
    send_task = asyncio.create_task(manager.send_message(session.id, "long task"))
    await started.wait()
    run_id = store.read_meta(session.id).run_ids[-1]

    child_context = ExecutionContext("child", "child", 1)
    unrelated_context = ExecutionContext("unrelated", "unrelated", 1)
    child_task = asyncio.create_task(asyncio.Event().wait())
    unrelated_task = asyncio.create_task(asyncio.Event().wait())
    registry.register("child", child_task, child_context, parent_run_id=run_id)
    registry.register("unrelated", unrelated_task, unrelated_context, parent_run_id="other")

    await manager.cancel_run(run_id)
    await send_task

    assert child_task.cancelled()
    assert child_context.reason == "cancelled"
    assert not unrelated_task.done()
    await registry.cancel_all()


async def test_cancel_unknown_or_finished_run_is_rejected(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    with pytest.raises(HandlerError) as error:
        await manager.cancel_run("run-missing")
    assert error.value.code == RUN_NOT_ACTIVE

    session = await manager.create("chat")
    run_id = await manager.send_message(session.id, "quick")
    with pytest.raises(HandlerError) as finished_error:
        await manager.cancel_run(run_id)
    assert finished_error.value.code == RUN_NOT_ACTIVE


async def test_session_lifecycle_rename_fork_export_delete(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]
    source = await manager.create("chat", "source")
    await manager.send_message(source.id, "hello")
    store.append_note(source.id, "shared context", "run-note")

    renamed = await manager.rename(source.id, "  renamed  ")
    forked = await manager.fork(source.id)
    filename, media_type, exported = await manager.export(forked.id, "json")
    await manager.delete(source.id)

    assert renamed.title == "renamed"
    assert forked.parent_session_id == source.id
    assert forked.status == "waiting_for_input"
    assert forked.run_ids == []
    assert store.read_messages(forked.id)[0]["content"] == "hello"
    assert "shared context" in store.read_notes(forked.id)
    assert filename == f"{forked.id}.json"
    assert media_type == "application/json"
    assert f'"parent_session_id": "{source.id}"' in exported
    assert not store.session_dir(source.id).exists()
    assert [session.id for session in await manager.list_sessions(include_closed=True)] == [
        forked.id
    ]
    event_types = [getattr(event, "type", "") for event in events]
    assert "session.renamed" in event_types
    assert "session.forked" in event_types
    assert "session.deleted" in event_types


async def test_session_mutations_reject_busy_session(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class _BlockingRunner(_Runner):
        async def run_and_capture(self, *args: object, **kwargs: object) -> RunOutcome:
            started.set()
            await release.wait()
            return RunOutcome(status="success", result="done", reason=None)

    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _BlockingRunner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    send_task = asyncio.create_task(manager.send_message(session.id, "work"))
    await started.wait()

    operations = [
        lambda: manager.rename(session.id, "new"),
        lambda: manager.fork(session.id),
        lambda: manager.export(session.id, "markdown"),
        lambda: manager.delete(session.id),
    ]
    for operation in operations:
        with pytest.raises(HandlerError) as error:
            await operation()
        assert error.value.code == SESSION_BUSY

    release.set()
    await send_task


# 功能：验证 Plan turn 将模式传给 runner，并在成功后先发布 plan.ready 再等待输入
# 设计：用捕获型 runner 返回固定计划，检查事件载荷与顺序，不依赖真实模型或 TUI
async def test_plan_turn_publishes_reviewable_plan(tmp_path: Path) -> None:
    events: list[object] = []
    seen_modes: list[RuntimeMode] = []
    bus = EventBus()

    # 收集计划生命周期事件
    async def collect(event: object) -> None:
        events.append(event)

    class _PlanRunner(_Runner):
        # 返回固定计划并记录 SessionManager 传入的运行模式
        async def run_and_capture(
            self,
            goal: str,
            **kwargs: object,
        ) -> RunOutcome:
            seen_modes.append(kwargs["runtime_mode"])  # type: ignore[arg-type]
            return RunOutcome(
                status="success",
                result="1. Inspect\n2. Implement\n3. Test",
                reason=None,
            )

    bus.subscribe(collect)  # type: ignore[arg-type]
    manager = SessionManager(
        SessionStore(tmp_path),
        lambda: _PlanRunner(),
        bus,
    )  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(
        session.id,
        "重构权限模块",
        runtime_mode=RuntimeMode.PLAN,
    )

    assert seen_modes == [RuntimeMode.PLAN]
    plan_event = next(event for event in events if getattr(event, "type", "") == "plan.ready")
    assert plan_event.run_id == run_id  # type: ignore[attr-defined]
    assert plan_event.request == "重构权限模块"  # type: ignore[attr-defined]
    assert "Implement" in plan_event.plan  # type: ignore[attr-defined]
    event_types = [getattr(event, "type", "") for event in events]
    assert event_types.index("plan.ready") < event_types.index("session.waiting_for_input")


# 功能：验证 Task Router 的 plan_first 会进入持久审批，而不是在原 Turn 内解锁写工具
# 设计：fake runner 直接追加可信 task.profiled 与 plan.updated 事件，检查待审批阻断和票据随批准返回
async def test_strategy_plan_requires_user_approval_before_next_turn(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    bus = EventBus()
    store = SessionStore(tmp_path / "sessions")

    # 收集计划卡与批准事件，验证用户可见流程绑定同一票据
    async def collect(event: object) -> None:
        events.append(event)

    class _StrategyPlanRunner(_Runner):
        # 写入策略和计划事实，模拟真实 Runner 的 SessionLedgerBridge
        async def run_and_capture(
            self,
            goal: str,
            **kwargs: object,
        ) -> RunOutcome:
            session = kwargs["session"]
            run_id = kwargs["run_id"]
            ledger = kwargs["store"]
            assert isinstance(session, Session)
            assert isinstance(run_id, str)
            assert isinstance(ledger, SessionStore)
            ledger.append_session_event(
                session.id,
                event_type="task.profiled",
                turn_id=run_id,
                payload={"profile": {"strategy": "plan_first"}},
            )
            ledger.append_session_event(
                session.id,
                event_type="plan.updated",
                turn_id=run_id,
                payload={
                    "explanation": "先确认修改边界",
                    "plan": [
                        {"step": "读取目标实现", "status": "completed"},
                        {"step": "修改并验证", "status": "pending"},
                    ],
                    "plan_ticket": "ticket-123",
                },
            )
            return RunOutcome(status="success", result="plan proposed", reason=None)

    bus.subscribe(collect)  # type: ignore[arg-type]
    manager = SessionManager(store, lambda: _StrategyPlanRunner(), bus)  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "帮我处理一下")

    plan_ready = next(
        event for event in events if getattr(event, "type", "") == "plan.ready"
    )
    assert getattr(plan_ready, "run_id") == run_id
    assert getattr(plan_ready, "plan_ticket") == "ticket-123"
    assert "修改并验证" in getattr(plan_ready, "plan")
    with pytest.raises(HandlerError, match="pending plan"):
        await manager.send_message(session.id, "绕过审批继续")

    resolved = await manager.respond_plan(session.id, run_id, "approve")

    assert resolved.plan_ticket == "ticket-123"


# 功能：验证任务与 context 查询读取最近一次 run，且空目录不会伪造数据
# 设计：直接构造持久化任务和 transcript，检查排序、消息计数和确定性 token 估算
async def test_session_task_and_context_views_use_latest_run(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    session.run_ids.extend(["run-old", "run-latest"])
    store.write_meta(session)
    tasks_dir = store.runs_dir(session.id) / "run-latest" / ".tasks"
    tasks_dir.mkdir(parents=True)
    for task_id, subject in [(2, "第二项"), (1, "第一项")]:
        (tasks_dir / f"task_{task_id}.json").write_text(
            json.dumps(
                {
                    "id": task_id,
                    "subject": subject,
                    "description": "",
                    "status": "pending",
                    "blocked_by": [],
                    "created_at": "t",
                    "updated_at": "t",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    store.append_message(session.id, "user", "检查任务", run_id="run-latest")

    run_id, tasks = manager.list_tasks(session.id)
    context = manager.context_info(session.id)

    assert run_id == "run-latest"
    assert [task["id"] for task in tasks] == [1, 2]
    assert context["message_count"] == 1
    assert context["estimated_tokens"] > 0
    assert context["run_count"] == 2
    assert context["last_run_id"] == "run-latest"


# 功能：验证 checkpoint 列表与 rewind 始终绑定当前会话最近一次 run
# 设计：创建真实 checkpoint 和文件变更，替换 workspace 探测后经 SessionManager 恢复并核对文件内容
async def test_session_checkpoint_view_and_rewind_latest_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    boundary = WorkspaceBoundary(workspace)
    monkeypatch.setattr(WorkspaceBoundary, "current", classmethod(lambda cls: boundary))
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    session.run_ids.append("run-latest")
    store.write_meta(session)
    target = workspace / "value.txt"
    target.write_text("before", encoding="utf-8")
    mutation = FileMutation(target, b"before", b"after")
    checkpoint_store = CheckpointStore(
        store.runs_dir(session.id) / "run-latest" / ".checkpoints",
        boundary,
    )
    checkpoint_id = checkpoint_store.create([mutation], label="edit value")
    apply_file_transaction(workspace, [mutation])

    run_id, checkpoints = manager.list_checkpoints(session.id)
    preview = manager.preview_rewind(session.id, checkpoint_id)
    result = manager.rewind(
        session.id,
        checkpoint_id,
        expected_digest=str(preview["state_digest"]),
    )

    assert run_id == "run-latest"
    assert checkpoints[0]["checkpoint_id"] == checkpoint_id
    assert preview["restorable"] == ["value.txt"]
    assert result["restored"] == ["value.txt"]
    assert target.read_text(encoding="utf-8") == "before"


# 功能：验证计划决定写入 Runtime 后可跨 daemon 重启恢复且只接受当前 run 一次
# 设计：用真实 SQLite Runtime 跑 Plan turn，重建 SessionManager 后解决并检查阻断、顺序与幂等边界
async def test_plan_response_is_durable_across_manager_restart(tmp_path: Path) -> None:
    transcript = SessionStore(tmp_path / "sessions")
    runtime_path = tmp_path / "runtime.db"
    first_bus = EventBus()
    first_runtime = RuntimeService(
        RuntimeStore(runtime_path),
        workspace=Path.cwd(),
        bus=first_bus,
    )
    first_bus.subscribe(first_runtime.record_bus_event)
    first = SessionManager(
        transcript,
        lambda: _Runner(),
        first_bus,
        runtime_service=first_runtime,
    )  # type: ignore[arg-type]
    session = await first.create("chat")
    plan_run_id = await first.send_message(
        session.id,
        "inspect auth",
        runtime_mode=RuntimeMode.PLAN,
    )

    with pytest.raises(HandlerError, match="pending plan"):
        await first.send_message(session.id, "must be blocked")

    restarted_bus = EventBus()
    restarted_runtime = RuntimeService(
        RuntimeStore(runtime_path),
        workspace=Path.cwd(),
        bus=restarted_bus,
    )
    restarted = SessionManager(
        transcript,
        lambda: _Runner(),
        restarted_bus,
        runtime_service=restarted_runtime,
    )  # type: ignore[arg-type]

    with pytest.raises(HandlerError, match="does not match"):
        await restarted.respond_plan(session.id, "run-stale", "cancel")
    with pytest.raises(HandlerError, match="could not be persisted"):
        await restarted.respond_plan(session.id, plan_run_id, "cancel")
    assert "plan.resolved" not in [
        event.type for event in await restarted_runtime.list_events(session.id)
    ]

    restarted_bus.subscribe(restarted_runtime.record_bus_event)
    resolved = await restarted.respond_plan(session.id, plan_run_id, "cancel")
    events = await restarted_runtime.list_events(session.id)

    assert resolved.type == "plan.resolved"
    assert [event.type for event in events].index("plan.ready") < [
        event.type for event in events
    ].index("plan.resolved")
    with pytest.raises(HandlerError, match="not pending"):
        await restarted.respond_plan(session.id, plan_run_id, "cancel")
    next_run_id = await restarted.send_message(session.id, "continue safely")
    assert next_run_id != plan_run_id

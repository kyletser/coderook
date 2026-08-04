from __future__ import annotations

from pathlib import Path

from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    SandboxCapability,
    ToolAction,
    WorkspaceTrust,
)
from code_rook.core.bus.events import RuntimeEventAppendedEvent
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.routes import RouteReceipt
from code_rook.core.runner import RunOutcome
from code_rook.core.runtime.models import ThreadStatus, TurnStatus
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.task.manager import TaskManager


class _Runner:
    # 模拟成功的 AgentRunner 并把 assistant 正文写回兼容 transcript
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
        store.append_message(
            session.id,
            "assistant",
            f"done {goal}",
            run_id=run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 创建隔离的 runtime service
def _service(tmp_path: Path) -> tuple[RuntimeService, RuntimeStore]:
    store = RuntimeStore(tmp_path / "runtime.db")
    return RuntimeService(store, workspace=tmp_path), store


# 功能：验证历史 session 与 run 索引可幂等导入 runtime
# 设计：连续 bootstrap 同一 interrupted session，确认 thread/facade/turn 不重复且末次 run 保留中断态
async def test_bootstrap_sessions_is_idempotent(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    session = Session(
        id="sess-history",
        mode="chat",
        status="interrupted",
        title="History",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T01:00:00Z",
        run_ids=["run-1", "run-2"],
    )

    await service.bootstrap_sessions([session])
    await service.bootstrap_sessions([session])

    assert store.get_thread(session.id).status == ThreadStatus.INTERRUPTED
    assert store.get_session_facade(session.id).mode == "chat"
    assert [turn.status for turn in store.list_turns(session.id)] == [
        TurnStatus.COMPLETED,
        TurnStatus.INTERRUPTED,
    ]


# 功能：验证 session send_message 通过 runtime service 建立并完成同一 turn
# 设计：使用真实 SessionManager 和双存储，检查兼容 JSONL 与 runtime thread/item/event 同时保持一致
async def test_session_message_projects_to_runtime(tmp_path: Path) -> None:
    service, runtime_store = _service(tmp_path)
    session_store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        session_store,
        lambda: _Runner(),
        EventBus(),
        runtime_service=service,
    )  # type: ignore[arg-type]

    session = await manager.create("chat")
    run_id = await manager.send_message(session.id, "hello")

    thread = runtime_store.get_thread(session.id)
    turn = runtime_store.get_turn(run_id)
    items = runtime_store.list_items(run_id)
    events = runtime_store.list_events(session.id)
    assert thread.status == ThreadStatus.IDLE
    assert thread.title == "hello"
    assert turn.status == TurnStatus.COMPLETED
    assert items[0].payload == {"role": "user", "content": "hello"}
    assert [event.type for event in events] == ["turn.started", "turn.completed"]
    assert [message["role"] for message in session_store.read_messages(session.id)] == [
        "user",
        "assistant",
    ]


# 功能：验证 session rename、fork、close 和 delete 会同步 runtime 投影
# 设计：串联现有用户操作，分别检查 thread 标题、父关系、归档态和级联删除
async def test_session_mutations_sync_runtime_projection(tmp_path: Path) -> None:
    service, runtime_store = _service(tmp_path)
    manager = SessionManager(
        SessionStore(tmp_path / "sessions"),
        lambda: _Runner(),
        EventBus(),
        runtime_service=service,
    )  # type: ignore[arg-type]
    source = await manager.create("chat", "source")

    await manager.rename(source.id, "renamed")
    forked = await manager.fork(source.id, "fork")
    await manager.close(source.id)

    assert runtime_store.get_thread(source.id).title == "renamed"
    assert runtime_store.get_thread(source.id).status == ThreadStatus.ARCHIVED
    assert runtime_store.get_session_facade(forked.id).parent_thread_id == source.id

    await manager.delete(source.id)

    assert not await service.contains_thread(source.id)


# 功能：验证 RuntimeService 为新 turn 写入 boot_id 并按持久 seq 发布 runtime 事件
# 设计：使用真实 EventBus 收集包装事件，依次启动和完成 turn，核对数据库与进程内事件顺序
async def test_runtime_service_publishes_durable_events(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    bus = EventBus()
    published: list[RuntimeEventAppendedEvent] = []

    # 收集 runtime 包装事件供顺序断言
    async def collect(event: RuntimeEventAppendedEvent) -> None:
        published.append(event)

    bus.subscribe(collect)  # type: ignore[arg-type]
    service = RuntimeService(
        store,
        workspace=tmp_path,
        bus=bus,
        boot_id="boot-test",
    )
    session = Session(
        id="sess-1",
        mode="chat",
        status="active",
        title="test",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
    )

    turn = await service.start_turn(session, "run-1", "hello")
    session.status = "waiting_for_input"
    session.updated_at = "2026-07-30T00:00:01Z"
    await service.finish_turn(session, "run-1", TurnStatus.COMPLETED)

    assert turn.boot_id == "boot-test"
    assert [event.seq for event in published] == [1, 2]
    assert [event.event_type for event in published] == [
        "turn.started",
        "turn.completed",
    ]


# 功能：验证 Task timeline 在 daemon 重启后仍能从 runtime event 查询
# 设计：启动真实 turn 并通过 TaskManager sink 写事件，再重建 store 查询持久 SQLite
async def test_task_timeline_projects_to_durable_runtime_events(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    session = Session(
        id="sess-task",
        mode="chat",
        status="active",
        title="task",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
    )
    await service.start_turn(session, "run-task", "work")
    manager = TaskManager(
        tmp_path / "tasks",
        event_sink=service.task_event_sink(session.id, "run-task"),
    )

    manager.create("durable")
    manager.claim(1, "executor")
    manager.add_artifact(1, name="report", uri="artifact://report")

    restarted_store = RuntimeStore(store.path)
    task_events = [
        event
        for event in restarted_store.list_events(session.id)
        if event.type.startswith("task.")
    ]
    assert [event.type for event in task_events] == [
        "task.created",
        "task.claimed",
        "task.artifact_added",
    ]
    assert all(event.payload["task_id"] == 1 for event in task_events)


# 功能：验证 turn 启动时把实际 RouteReceipt 同时写入 TurnRecord 与 durable event
# 设计：使用包含 origin 和凭据来源的冻结 receipt，往返读取数据库并核对事件 payload 无密钥字段
async def test_start_turn_persists_actual_route_receipt(tmp_path: Path) -> None:
    service, store = _service(tmp_path)
    session = Session(
        id="sess-route",
        mode="chat",
        status="active",
        title="route",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
    )
    receipt = RouteReceipt(
        route_id="openai-work",
        wire_format="openai_responses",
        base_url_origin="https://api.openai.com",
        model="gpt-test",
        credential_source="keyring",
    )

    await service.start_turn(session, "run-route", "hello", route=receipt)

    persisted = store.get_turn("run-route")
    event = store.list_events(session.id)[0]
    assert persisted.route == receipt
    assert event.payload["route"] == receipt.model_dump(mode="json")
    assert "api_key" not in event.model_dump_json()


# 功能：验证 turn 启动时冻结 session 的 mode、authority、trust、sandbox 和 action scope
# 设计：provider 返回非默认快照，启动后修改原变量不影响 SQLite 中已保存的 TurnRecord
async def test_start_turn_freezes_authority_snapshot(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    snapshot = AuthoritySnapshot(
        mode=RuntimeMode.OPERATE,
        profile=AuthorityProfile.AUTO_REVIEW,
        workspace_trust=WorkspaceTrust.TRUSTED,
        sandbox=SandboxCapability(
            available=False,
            kind="windows_none",
            reason="no OS isolation backend",
        ),
        allowed_actions=frozenset({ToolAction.READ, ToolAction.MUTATE}),
    )

    # 返回当前 session 下一 turn 应冻结的权限快照
    def authority_for_session(_session_id: str) -> AuthoritySnapshot:
        return snapshot

    service = RuntimeService(
        store,
        workspace=tmp_path,
        authority_provider=authority_for_session,
    )
    session = Session(
        id="sess-authority",
        mode="chat",
        status="active",
        title="authority",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
    )

    await service.start_turn(session, "run-authority", "inspect")

    persisted = store.get_turn("run-authority")
    assert persisted.authority_snapshot == snapshot


# 功能：验证单次 Plan 请求会覆盖 turn mode 但保留其余 session authority 字段
# 设计：provider 返回 Full Access 快照，再用 runtime_mode=plan 启动 turn，检查只替换 mode
async def test_start_turn_applies_per_turn_plan_mode(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.db")
    snapshot = AuthoritySnapshot(
        mode=RuntimeMode.ACT,
        profile=AuthorityProfile.FULL_ACCESS,
        workspace_trust=WorkspaceTrust.TRUSTED,
        allowed_actions=frozenset({ToolAction.READ, ToolAction.MUTATE}),
    )
    service = RuntimeService(
        store,
        workspace=tmp_path,
        authority_provider=lambda _session_id: snapshot,
    )
    session = Session(
        id="sess-plan",
        mode="chat",
        status="active",
        title="plan",
        created_at="2026-07-30T00:00:00Z",
        updated_at="2026-07-30T00:00:00Z",
    )

    await service.start_turn(
        session,
        "run-plan",
        "plan changes",
        runtime_mode=RuntimeMode.PLAN,
    )

    persisted = store.get_turn("run-plan")
    assert persisted.mode == RuntimeMode.PLAN
    assert persisted.authority_profile == AuthorityProfile.FULL_ACCESS
    assert persisted.allowed_actions == snapshot.allowed_actions

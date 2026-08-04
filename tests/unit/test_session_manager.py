from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.context import ExecutionContext
from code_rook.core.editing import FileMutation, apply_file_transaction
from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.runner import RunOutcome
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


# 功能：daemon 冷启动时恢复 meta 索引，并把运行中的会话标记为 interrupted
# 设计：预写 active meta 后重建 manager，证明异常退出状态可被用户识别和恢复
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
    assert list(store.session_dir("sess-recover").glob("thread_interrupted_*.jsonl"))


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
    result = manager.rewind(session.id, checkpoint_id)

    assert run_id == "run-latest"
    assert checkpoints[0]["checkpoint_id"] == checkpoint_id
    assert result["restored"] == ["value.txt"]
    assert target.read_text(encoding="utf-8") == "before"

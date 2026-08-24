from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_rook.core.authority import AuthorityProfile, RuntimeMode, WorkspaceTrust
from code_rook.core.bus.commands import (
    AgentRunCommand,
    BackgroundCancelCommand,
    BackgroundGetCommand,
    CoreAuthenticateCommand,
    CoreAuthenticateResult,
    EventReplayCommand,
    EventSubscribeCommand,
    EventUnsubscribeCommand,
    GoalContinueDecisionCommand,
    GoalCreateCommand,
    HookRerunCommand,
    HooksListCommand,
    McpListCommand,
    MemoryAddCommand,
    MemoryDeleteCommand,
    MemoryEditCommand,
    MemoryExpireCommand,
    MemoryListCommand,
    MemoryPinCommand,
    MemorySettingsSetCommand,
    PingCommand,
    PlanRespondCommand,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    RunSteerCommand,
    RuntimeCapabilitiesCommand,
    RuntimeCapabilitiesResult,
    SessionCheckpointsCommand,
    SessionContextCommand,
    SessionDeleteCommand,
    SessionExportCommand,
    SessionForkCommand,
    SessionGetAuthorityCommand,
    SessionListCommand,
    SessionRenameCommand,
    SessionResumeCommand,
    SessionRewindCommand,
    SessionRewindPreviewCommand,
    SessionSendMessageCommand,
    SessionSetAuthorityCommand,
    SessionTasksCommand,
    ThreadArchiveCommand,
    ThreadCreateCommand,
    ThreadGetCommand,
    ThreadListCommand,
    ThreadUpdateCommand,
    TurnGetCommand,
    TurnInspectCommand,
    TurnInterruptCommand,
    TurnItemsCommand,
    TurnListCommand,
    TurnStartCommand,
    TurnSteerCommand,
    UserQuestionRespondCommand,
    WorkerApplyCommand,
    WorkerCancelCommand,
    WorkerEventsCommand,
    WorkerFollowupCommand,
    WorkerListCommand,
    WorkerRetryCommand,
    WorkerReviewCommand,
    WorkerStartCommand,
    WorkerStatusCommand,
    WorkflowGetCommand,
    WorkflowListCommand,
    WorkflowStartCommand,
    WorkspaceCommitCommand,
    WorkspaceDiffCommand,
    WorkspaceStageCommand,
)
from code_rook.core.bus.events import (
    AgentDecisionEvent,
    ContextBudgetEvent,
    CoreStartedEvent,
    GoalContinueDecisionEvent,
    PlanReadyEvent,
    PlanResolvedEvent,
    RunSteeredEvent,
    SessionInterruptedEvent,
    UserQuestionAskedEvent,
)


# 功能：验证 turn inspector 命令和 context budget 事件的 typed wire 字段可往返
# 设计：同时覆盖新增请求判别值与 schema token 开销事件，防止 Core/TUI 协议字段漂移
def test_turn_inspect_and_context_budget_protocol() -> None:
    command = TurnInspectCommand(turn_id="run-1")
    budget = ContextBudgetEvent(
        run_id="run-1",
        step=2,
        message_tokens=100,
        system_tokens=200,
        tool_schema_tokens=300,
        tool_count=12,
        ts="2026-08-04T00:00:00Z",
    )

    assert TurnInspectCommand.model_validate_json(command.model_dump_json()).type == "turn.inspect"
    assert ContextBudgetEvent.model_validate_json(
        budget.model_dump_json()
    ).tool_schema_tokens == 300


# 功能：Worker 控制中心命令保留事件游标、followup 与显式确认的 review 契约
# 设计：直接 JSON 往返三个模型并断言 review 默认未确认，防止客户端静默批准 handoff
def test_worker_control_commands_roundtrip() -> None:
    start = WorkerStartCommand(
        session_id="sess-1",
        description="inspect",
        prompt="inspect repository",
        model="worker-model",
    )
    status = WorkerStatusCommand(session_id="sess-1", worker_id="worker-1")
    retry = WorkerRetryCommand(session_id="sess-1", worker_id="worker-1")
    events = WorkerEventsCommand(
        session_id="sess-1", worker_id="worker-1", after_cursor=9
    )
    followup = WorkerFollowupCommand(
        session_id="sess-1", worker_id="worker-1", message="rerun tests"
    )
    review = WorkerReviewCommand(
        session_id="sess-1", worker_id="worker-1", approved=True
    )
    apply = WorkerApplyCommand(
        session_id="sess-1",
        worker_id="worker-1",
        expected_digest="a" * 64,
        confirmed=True,
    )

    assert WorkerEventsCommand.model_validate_json(
        events.model_dump_json()
    ).after_cursor == 9
    assert WorkerFollowupCommand.model_validate_json(
        followup.model_dump_json()
    ).message == "rerun tests"
    assert WorkerReviewCommand.model_validate_json(
        review.model_dump_json()
    ).confirmed is False
    assert WorkerApplyCommand.model_validate_json(
        apply.model_dump_json()
    ).expected_digest == "a" * 64
    assert WorkerStartCommand.model_validate_json(start.model_dump_json()).read_only is True
    assert WorkerStartCommand.model_validate_json(start.model_dump_json()).model == "worker-model"
    assert WorkerStatusCommand.model_validate_json(
        status.model_dump_json()
    ).worker_id == "worker-1"
    assert WorkerRetryCommand.model_validate_json(
        retry.model_dump_json()
    ).session_id == "sess-1"
    for command_model, payload in (
        (WorkerListCommand, {}),
        (WorkerEventsCommand, {"worker_id": "worker-1"}),
        (
            WorkerFollowupCommand,
            {"worker_id": "worker-1", "message": "rerun tests"},
        ),
        (WorkerReviewCommand, {"worker_id": "worker-1", "approved": True}),
        (WorkerCancelCommand, {"worker_id": "worker-1"}),
    ):
        with pytest.raises(ValidationError):
            command_model.model_validate(payload)
    with pytest.raises(ValidationError):
        WorkerApplyCommand(
            session_id="sess-1",
            worker_id="worker-1",
            expected_digest="stale",
        )


# 功能：验证 headless 提问策略协议拒绝缺失超时或预置答案的无界配置
# 设计：分别构造合法与两个非法组合，固定 daemon 在启动 run 前 fail closed 的边界
def test_agent_run_question_policy_is_bounded() -> None:
    valid = AgentRunCommand(
        goal="work",
        question_mode="timeout",
        question_timeout_s=10,
    )

    assert valid.question_timeout_s == 10
    with pytest.raises(ValidationError):
        AgentRunCommand(goal="work", question_mode="timeout")
    with pytest.raises(ValidationError):
        AgentRunCommand(goal="work", question_mode="preset")


# 功能：验证有限 Goal Loop 的 typed 创建参数、决策命令和决策事件可稳定往返
# 设计：使用默认安全上限构造命令，再序列化包含预算余量的事件以固定 wire 字段
def test_bounded_goal_loop_protocol_roundtrip() -> None:
    create = GoalCreateCommand(
        session_id="sess-1",
        objective="ship",
        auto_continue=True,
    )
    command = GoalContinueDecisionCommand(session_id="sess-1")
    event = GoalContinueDecisionEvent(
        goal_id="goal-123456789abc",
        session_id="sess-1",
        run_id="run-1",
        should_continue=True,
        reason="ready_for_bounded_continuation",
        auto_turns_used=1,
        remaining_auto_turns=2,
        tokens_used=100,
        token_budget=1000,
        remaining_tokens=900,
        wall_elapsed_seconds=30,
        max_wall_seconds=1800,
        paused_needs_confirmation=False,
        ts="2026-08-24T00:00:00Z",
    )

    assert create.max_auto_turns == 3
    assert create.max_wall_seconds == 1800
    assert command.type == "goal.continue_decision"
    assert GoalContinueDecisionEvent.model_validate_json(
        event.model_dump_json()
    ).remaining_tokens == 900


# 功能：验证旧三字段 RuntimeCapabilitiesResult 构造仍有效并自动补齐新增协商字段
# 设计：仅传 version/runtime_modes/features 的旧 payload，固定 additive schema 的向后兼容行为
def test_runtime_capabilities_legacy_payload_remains_valid() -> None:
    restored = RuntimeCapabilitiesResult(
        version="0.2.0",
        runtime_modes=[RuntimeMode.ACT],
        features=["durable_threads"],
    )

    assert restored.features == ["durable_threads"]
    assert restored.runtime_event_schema_version == 1
    assert restored.stream_json_schema_versions == [1]
    assert "stable" in restored.feature_flags.model_dump()


# 功能：R1 thread/turn/runtime 命令清单全部具有精确的 typed 判别值
# 设计：逐个构造规范要求的十二个操作，固定兼容 session 之外的正式 runtime 协议面
def test_runtime_contract_protocol_commands_are_complete() -> None:
    commands = [
        ThreadCreateCommand(title="Thread"),
        ThreadListCommand(),
        ThreadGetCommand(thread_id="thread-1"),
        ThreadUpdateCommand(thread_id="thread-1", title="Renamed"),
        ThreadArchiveCommand(thread_id="thread-1"),
        TurnStartCommand(thread_id="thread-1", content="Work"),
        TurnGetCommand(turn_id="turn-1"),
        TurnListCommand(thread_id="thread-1"),
        TurnInterruptCommand(turn_id="turn-1"),
        TurnSteerCommand(turn_id="turn-1", content="Adjust"),
        TurnItemsCommand(turn_id="turn-1"),
        RuntimeCapabilitiesCommand(),
    ]

    assert [command.type for command in commands] == [
        "thread.create",
        "thread.list",
        "thread.get",
        "thread.update",
        "thread.archive",
        "turn.start",
        "turn.get",
        "turn.list",
        "turn.interrupt",
        "turn.steer",
        "turn.items",
        "runtime.capabilities",
    ]


# 功能：验证 PingCommand 序列化后再反序列化，client 和 type 字段完整保留
# 设计：JSON 往返测试确认 wire 协议的序列化正确性，type 字段是 discriminated union 的判别键
def test_ping_command_roundtrip() -> None:
    cmd = PingCommand(client="cli/0.0.1")
    cmd2 = PingCommand.model_validate_json(cmd.model_dump_json())
    assert cmd2.client == "cli/0.0.1"
    assert cmd2.type == "core.ping"


def test_core_authentication_protocol_models() -> None:
    command = CoreAuthenticateCommand(token="x" * 43)
    result = CoreAuthenticateResult()

    assert command.type == "core.authenticate"
    assert CoreAuthenticateCommand.model_validate_json(
        command.model_dump_json()
    ).token == "x" * 43
    assert result.authenticated is True


# 功能：验证 PingCommand 的 type 字段默认值为 "core.ping"
# 设计：Literal 默认值测试，type 是 Command union 的判别键，必须与 union 定义完全一致，否则反序列化时会路由到错误类型
def test_ping_command_default_type() -> None:
    cmd = PingCommand(client="x")
    assert cmd.type == "core.ping"


# 功能：验证缺少必填 client 字段时 pydantic 校验失败
# 设计：传入空 dict 触发校验，确认 client 是必填字段，防止 daemon 收到不完整的 ping 命令进入 handler
def test_ping_command_missing_client_raises() -> None:
    with pytest.raises(ValidationError):
        PingCommand.model_validate({})


# 功能：验证 PongResult 序列化往返后所有字段完整保留
# 设计：与 PingCommand 对称，测试命令-响应对的两端序列化，确认 int 和 str 字段类型在往返中不变
def test_pong_result_roundtrip() -> None:
    pong = PongResult(
        server_version="0.0.1",
        uptime_ms=42,
        received_at="2026-05-11T00:00:00Z",
        workspace="/repo",
        active_runs=1,
    )
    pong2 = PongResult.model_validate(pong.model_dump())
    assert pong2.server_version == "0.0.1"
    assert pong2.uptime_ms == 42
    assert pong2.workspace == "/repo"
    assert pong2.active_runs == 1


# 功能：验证 CoreStartedEvent 序列化往返后 listen_addr 和 type 字段正确保留
# 设计：CoreStartedEvent 是 daemon 启动通知，往返测试确认 type 的 Literal 约束在反序列化后保持（不被字段名覆盖）
def test_core_started_event_roundtrip() -> None:
    evt = CoreStartedEvent(listen_addr="127.0.0.1:7437", version="0.0.1")
    evt2 = CoreStartedEvent.model_validate_json(evt.model_dump_json())
    assert evt2.listen_addr == "127.0.0.1:7437"
    assert evt2.type == "core.started"


# 功能：验证 Agent 决策事件完整承载动作意图、用户可见摘要和工具列表
# 设计：执行 JSON 往返并检查 Literal 分类，保证 Core、TUI 与事件日志共享同一协议
def test_agent_decision_event_roundtrip() -> None:
    event = AgentDecisionEvent(
        run_id="run-1",
        step=2,
        intent="inspect",
        summary="我先检查运行状态。",
        tool_names=["bash"],
        has_visible_text=True,
        ts="2026-07-30T00:00:00Z",
    )

    restored = AgentDecisionEvent.model_validate_json(event.model_dump_json())

    assert restored.type == "agent.decision"
    assert restored.intent == "inspect"
    assert restored.tool_names == ["bash"]


# 功能：验证 session list/resume 的 wire command 字段和范围约束
def test_session_recovery_commands_validate() -> None:
    listed = SessionListCommand(limit=20, include_closed=True)
    resumed = SessionResumeCommand(session_id="sess-abc")

    assert listed.type == "session.list"
    assert listed.limit == 20
    assert resumed.type == "session.resume"
    with pytest.raises(ValidationError):
        SessionListCommand(limit=0)


def test_session_lifecycle_commands_validate() -> None:
    renamed = SessionRenameCommand(session_id="sess-1", title="new title")
    forked = SessionForkCommand(session_id="sess-1")
    exported = SessionExportCommand(session_id="sess-1", format="json")
    deleted = SessionDeleteCommand(session_id="sess-1")

    assert renamed.type == "session.rename"
    assert forked.type == "session.fork"
    assert exported.format == "json"
    assert deleted.type == "session.delete"
    with pytest.raises(ValidationError):
        SessionRenameCommand(session_id="sess-1", title="")
    with pytest.raises(ValidationError):
        SessionExportCommand(session_id="sess-1", format="xml")  # type: ignore[arg-type]


def test_run_cancel_protocol_roundtrip() -> None:
    command = RunCancelCommand(run_id="run-123")
    result = RunCancelResult(run_id="run-123", session_id="sess-123")
    event = SessionInterruptedEvent(
        session_id="sess-123",
        last_run_id="run-123",
        ts="2026-07-16T00:00:00Z",
    )

    assert RunCancelCommand.model_validate_json(command.model_dump_json()).run_id == "run-123"
    assert result.status == "cancelled"
    assert event.type == "session.interrupted"
    assert event.reason == "cancelled"


# 功能：验证运行中纠偏命令和事件携带稳定的 run/session 绑定
# 设计：对命令与服务端事件分别做 JSON 往返，防止纠偏被投递到错误会话或活动 run
def test_run_steering_protocol_roundtrip() -> None:
    command = RunSteerCommand(run_id="run-1", content="保留旧接口")
    event = RunSteeredEvent(
        run_id="run-1",
        session_id="sess-1",
        content="保留旧接口",
        ts="2026-07-31T00:00:00Z",
    )

    restored_command = RunSteerCommand.model_validate_json(command.model_dump_json())
    restored_event = RunSteeredEvent.model_validate_json(event.model_dump_json())

    assert restored_command.type == "run.steer"
    assert restored_command.content == "保留旧接口"
    assert restored_event.session_id == "sess-1"


# 功能：验证结构化问题事件和回答命令支持选项、多选标识与自由文本答案
# 设计：执行双向协议模型往返，固定 Agent 工具、Core 与 TUI 之间的字段契约
def test_user_question_protocol_roundtrip() -> None:
    event = UserQuestionAskedEvent(
        question_id="question-1",
        run_id="run-1",
        session_id="sess-1",
        question="选择数据库？",
        header="数据库",
        options=["SQLite", "PostgreSQL"],
        multi_select=False,
        ts="2026-07-31T00:00:00Z",
    )
    command = UserQuestionRespondCommand(
        question_id="question-1",
        answer="SQLite",
    )

    restored_event = UserQuestionAskedEvent.model_validate_json(event.model_dump_json())
    restored_command = UserQuestionRespondCommand.model_validate_json(
        command.model_dump_json()
    )

    assert restored_event.type == "user_question.asked"
    assert restored_event.options == ["SQLite", "PostgreSQL"]
    assert restored_command.type == "user_question.respond"
    assert restored_command.answer == "SQLite"


def test_agent_run_headless_permission_protocol() -> None:
    default_command = AgentRunCommand(goal="inspect")
    allow_list = AgentRunCommand(
        goal="edit",
        permission_mode="allow_list",
        allow_tools=["edit_file", "bash"],
    )

    assert default_command.permission_mode == "fail_fast"
    assert default_command.allow_tools == []
    assert AgentRunCommand.model_validate_json(
        allow_list.model_dump_json()
    ).allow_tools == ["edit_file", "bash"]


# 功能：验证 session 消息可显式携带 Plan Mode 且默认仍为 Act
# 设计：分别构造默认和计划命令并做 JSON 往返，固定客户端与 Core 的每轮模式契约
def test_session_message_runtime_mode_roundtrip() -> None:
    default = SessionSendMessageCommand(session_id="sess-1", content="implement")
    planned = SessionSendMessageCommand(
        session_id="sess-1",
        content="inspect and plan",
        runtime_mode=RuntimeMode.PLAN,
    )

    assert default.runtime_mode == RuntimeMode.ACT
    assert SessionSendMessageCommand.model_validate_json(
        planned.model_dump_json()
    ).runtime_mode == RuntimeMode.PLAN


# 功能：验证会话 authority 更新可独立携带 mode、profile 和 workspace trust
# 设计：执行 JSON 往返并覆盖三个非默认值，固定四维状态互不绑定的协议契约
def test_session_authority_commands_roundtrip() -> None:
    get_command = SessionGetAuthorityCommand(session_id="sess-1")
    set_command = SessionSetAuthorityCommand(
        session_id="sess-1",
        mode=RuntimeMode.PLAN,
        profile=AuthorityProfile.AUTO_REVIEW,
        workspace_trust=WorkspaceTrust.TRUSTED,
    )

    restored = SessionSetAuthorityCommand.model_validate_json(
        set_command.model_dump_json()
    )

    assert get_command.type == "session.get_authority"
    assert restored.type == "session.set_authority"
    assert restored.mode == RuntimeMode.PLAN
    assert restored.profile == AuthorityProfile.AUTO_REVIEW
    assert restored.workspace_trust == WorkspaceTrust.TRUSTED


# 功能：验证 authority 更新允许只改变一个维度并拒绝空更新
# 设计：分别构造仅 mode、仅 profile、仅 trust 和空命令，防止客户端被迫联动无关状态
def test_session_authority_partial_update_validation() -> None:
    assert SessionSetAuthorityCommand(
        session_id="sess-1", mode=RuntimeMode.OPERATE
    ).profile is None
    assert SessionSetAuthorityCommand(
        session_id="sess-1", profile=AuthorityProfile.FULL_ACCESS
    ).mode is None
    assert SessionSetAuthorityCommand(
        session_id="sess-1", workspace_trust=WorkspaceTrust.TRUSTED
    ).workspace_trust == WorkspaceTrust.TRUSTED
    with pytest.raises(ValidationError):
        SessionSetAuthorityCommand(session_id="sess-1")


# 功能：验证 tasks、workers、diff、checkpoint、rewind 和 context 高频视图命令具有稳定判别类型
# 设计：构造六个 typed command 并检查关键参数，确保 TUI 不依赖自由格式命令字符串
def test_session_inspection_commands_validate() -> None:
    tasks = SessionTasksCommand(session_id="sess-1")
    workers = WorkerListCommand(session_id="sess-1", limit=25)
    diff = WorkspaceDiffCommand(scope="unstaged", path="src")
    stage = WorkspaceStageCommand(
        session_id="sess-1",
        paths=["src/app.py"],
        expected_digest="a" * 64,
        confirmed=True,
    )
    commit = WorkspaceCommitCommand(
        session_id="sess-1",
        message="fix: verified change",
        expected_digest="b" * 64,
        confirmed=True,
    )
    checkpoints = SessionCheckpointsCommand(session_id="sess-1")
    rewind = SessionRewindCommand(
        session_id="sess-1",
        checkpoint_id="20260731T010203-abcdef12",
        expected_digest="c" * 64,
        confirmed=True,
    )
    rewind_preview = SessionRewindPreviewCommand(
        session_id="sess-1",
        checkpoint_id="20260731T010203-abcdef12",
    )
    context = SessionContextCommand(session_id="sess-1")
    workflow_start = WorkflowStartCommand(source="{}", format="json")
    workflow_list = WorkflowListCommand(limit=25)
    workflow_get = WorkflowGetCommand(workflow_id="release")

    assert tasks.type == "session.tasks"
    assert workers.type == "worker.list"
    assert workers.limit == 25
    assert diff.type == "workspace.diff"
    assert diff.scope == "unstaged"
    assert stage.type == "workspace.stage"
    assert stage.paths == ["src/app.py"]
    assert commit.type == "workspace.commit"
    assert commit.message == "fix: verified change"
    assert checkpoints.type == "session.checkpoints"
    assert rewind.type == "session.rewind"
    assert rewind_preview.type == "session.rewind_preview"
    assert context.type == "session.context"
    assert workflow_start.type == "workflow.start"
    assert workflow_list.type == "workflow.list"
    assert workflow_get.type == "workflow.get"
    with pytest.raises(ValidationError):
        WorkspaceStageCommand(
            session_id="sess-1",
            paths=[],
            expected_digest="a" * 64,
        )


# 功能：验证管理面板四件套命令具有精确 typed 判别值与范围约束
# 设计：构造 mcp/hooks/memory/background/worker 命令并做 JSON 往返，固定新协议面
def test_management_panel_commands_roundtrip() -> None:
    mcp = McpListCommand()
    hooks = HooksListCommand(limit=30)
    rerun = HookRerunCommand(hook_id="post-commit", session_id="sess-trusted")
    memories = MemoryListCommand()
    memory_add = MemoryAddCommand(name="rule", body="Run tests.")
    memory_edit = MemoryEditCommand(memory_id="m1", body="Run focused tests.")
    memory_pin = MemoryPinCommand(memory_id="m1")
    memory_expire = MemoryExpireCommand(memory_id="m1", expires_at=None)
    memory_del = MemoryDeleteCommand(memory_id="m1")
    memory_settings = MemorySettingsSetCommand(auto_save="off")
    background = BackgroundGetCommand(session_id="sess-1", job_id="j1")
    background_cancel = BackgroundCancelCommand(session_id="sess-1", job_id="j1")
    worker_cancel = WorkerCancelCommand(session_id="sess-1", worker_id="w1")

    assert McpListCommand.model_validate_json(mcp.model_dump_json()).type == "mcp.list"
    assert hooks.type == "hooks.list"
    assert hooks.limit == 30
    assert HookRerunCommand.model_validate_json(
        rerun.model_dump_json()
    ).hook_id == "post-commit"
    assert HookRerunCommand.model_validate_json(
        rerun.model_dump_json()
    ).session_id == "sess-trusted"
    assert memories.type == "memory.list"
    assert MemoryAddCommand.model_validate_json(
        memory_add.model_dump_json()
    ).memory_type == "project"
    assert MemoryEditCommand.model_validate_json(
        memory_edit.model_dump_json()
    ).body == "Run focused tests."
    assert memory_pin.pinned is True
    assert memory_expire.expires_at is None
    assert MemoryDeleteCommand.model_validate_json(
        memory_del.model_dump_json()
    ).memory_id == "m1"
    assert memory_settings.auto_save == "off"
    assert BackgroundGetCommand.model_validate_json(
        background.model_dump_json()
    ).job_id == "j1"
    assert background.session_id == "sess-1"
    assert background_cancel.type == "background.cancel"
    assert worker_cancel.type == "worker.cancel"
    assert worker_cancel.session_id == "sess-1"

    with pytest.raises(ValidationError):
        HookRerunCommand(hook_id="")
    with pytest.raises(ValidationError):
        HooksListCommand(limit=0)
    with pytest.raises(ValidationError):
        MemoryEditCommand(memory_id="m1")
    with pytest.raises(ValidationError):
        BackgroundGetCommand(job_id="j1")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        BackgroundCancelCommand(job_id="j1")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        WorkerCancelCommand(worker_id="w1")  # type: ignore[call-arg]


# 功能：验证计划完成事件携带原请求和完整计划供 TUI 审阅
# 设计：执行 JSON 往返并检查稳定 run/session 标识，防止计划被绑定到错误会话
def test_plan_ready_event_roundtrip() -> None:
    event = PlanReadyEvent(
        session_id="sess-1",
        run_id="run-plan",
        request="refactor auth",
        plan="1. inspect\n2. edit\n3. test",
        ts="2026-07-30T00:00:00Z",
    )

    restored = PlanReadyEvent.model_validate_json(event.model_dump_json())

    assert restored.type == "plan.ready"
    assert restored.session_id == "sess-1"
    assert restored.plan.startswith("1. inspect")


# 功能：验证 runtime 事件回放命令的游标范围和分页上限
# 设计：分别构造合法边界与越界参数，确保 wire 层在进入存储查询前拒绝错误值
def test_event_replay_command_validates_cursor_and_limit() -> None:
    command = EventReplayCommand(thread_id="thread-1", after_seq=4, limit=1000)

    assert command.type == "event.replay"
    with pytest.raises(ValidationError):
        EventReplayCommand(thread_id="thread-1", after_seq=-1)
    with pytest.raises(ValidationError):
        EventReplayCommand(thread_id="thread-1", limit=1001)


# 功能：验证 thread 订阅不能混用旧 run 回放，且非 thread 订阅不能携带游标
# 设计：覆盖两个歧义组合并保留合法 thread 游标，明确新旧订阅协议的互斥边界
def test_event_subscribe_replay_sources_are_unambiguous() -> None:
    command = EventSubscribeCommand(
        topics=["turn.*"],
        thread_id="thread-1",
        after_seq=2,
    )

    assert command.after_seq == 2
    with pytest.raises(ValidationError):
        EventSubscribeCommand(
            topics=["turn.*"],
            thread_id="thread-1",
            replay_from_run="run-1",
        )
    with pytest.raises(ValidationError):
        EventSubscribeCommand(topics=["turn.*"], after_seq=1)


# 功能：验证取消订阅命令携带稳定标识并拒绝空 subscription_id
# 设计：同时构造合法值与空字符串，让 typed wire 边界在进入 broadcaster 前完成约束
def test_event_unsubscribe_command_requires_subscription_id() -> None:
    command = EventUnsubscribeCommand(subscription_id="sub-thread-1")

    assert command.type == "event.unsubscribe"
    assert command.subscription_id == "sub-thread-1"
    with pytest.raises(ValidationError):
        EventUnsubscribeCommand(subscription_id="")


# 功能：验证计划决定命令与持久解决事件携带同一会话、run 和决定语义
# 设计：分别往返合法 revise 与拒绝 approve 混入 revision，固定 typed wire 的互斥字段
def test_plan_response_protocol_is_typed_and_unambiguous() -> None:
    command = PlanRespondCommand(
        session_id="sess-plan",
        run_id="run-plan",
        decision="revise",
        revision="inspect the migration path",
    )
    event = PlanResolvedEvent(
        session_id="sess-plan",
        run_id="run-plan",
        decision="revise",
        revision=command.revision,
        ts="2026-08-24T00:00:00Z",
    )

    assert PlanRespondCommand.model_validate_json(
        command.model_dump_json()
    ).decision == "revise"
    assert PlanResolvedEvent.model_validate_json(
        event.model_dump_json()
    ).revision == command.revision
    with pytest.raises(ValidationError):
        PlanRespondCommand(
            session_id="sess-plan",
            run_id="run-plan",
            decision="approve",
            revision="must not be ignored",
        )

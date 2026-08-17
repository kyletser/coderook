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
    HookRerunCommand,
    HooksListCommand,
    McpListCommand,
    MemoryDeleteCommand,
    MemoryListCommand,
    PingCommand,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    RunSteerCommand,
    RuntimeCapabilitiesCommand,
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
    WorkerCancelCommand,
    WorkerListCommand,
    WorkflowGetCommand,
    WorkflowListCommand,
    WorkflowStartCommand,
    WorkspaceDiffCommand,
)
from code_rook.core.bus.events import (
    AgentDecisionEvent,
    ContextBudgetEvent,
    CoreStartedEvent,
    PlanReadyEvent,
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
    pong = PongResult(server_version="0.0.1", uptime_ms=42, received_at="2026-05-11T00:00:00Z")
    pong2 = PongResult.model_validate(pong.model_dump())
    assert pong2.server_version == "0.0.1"
    assert pong2.uptime_ms == 42


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
    checkpoints = SessionCheckpointsCommand(session_id="sess-1")
    rewind = SessionRewindCommand(
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
    assert checkpoints.type == "session.checkpoints"
    assert rewind.type == "session.rewind"
    assert context.type == "session.context"
    assert workflow_start.type == "workflow.start"
    assert workflow_list.type == "workflow.list"
    assert workflow_get.type == "workflow.get"


# 功能：验证管理面板四件套命令具有精确 typed 判别值与范围约束
# 设计：构造 mcp/hooks/memory/background/worker 命令并做 JSON 往返，固定新协议面
def test_management_panel_commands_roundtrip() -> None:
    mcp = McpListCommand()
    hooks = HooksListCommand(limit=30)
    rerun = HookRerunCommand(hook_id="post-commit")
    memories = MemoryListCommand()
    memory_del = MemoryDeleteCommand(memory_id="m1")
    background = BackgroundGetCommand(job_id="j1")
    background_cancel = BackgroundCancelCommand(job_id="j1")
    worker_cancel = WorkerCancelCommand(worker_id="w1")

    assert McpListCommand.model_validate_json(mcp.model_dump_json()).type == "mcp.list"
    assert hooks.type == "hooks.list"
    assert hooks.limit == 30
    assert HookRerunCommand.model_validate_json(
        rerun.model_dump_json()
    ).hook_id == "post-commit"
    assert memories.type == "memory.list"
    assert MemoryDeleteCommand.model_validate_json(
        memory_del.model_dump_json()
    ).memory_id == "m1"
    assert BackgroundGetCommand.model_validate_json(
        background.model_dump_json()
    ).job_id == "j1"
    assert background_cancel.type == "background.cancel"
    assert worker_cancel.type == "worker.cancel"

    with pytest.raises(ValidationError):
        HookRerunCommand(hook_id="")
    with pytest.raises(ValidationError):
        HooksListCommand(limit=0)


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

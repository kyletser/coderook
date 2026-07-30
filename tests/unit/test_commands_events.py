from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_rook.core.authority import AuthorityProfile, RuntimeMode
from code_rook.core.bus.commands import (
    AgentRunCommand,
    CoreAuthenticateCommand,
    CoreAuthenticateResult,
    EventReplayCommand,
    EventSubscribeCommand,
    PingCommand,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    SessionDeleteCommand,
    SessionExportCommand,
    SessionForkCommand,
    SessionGetAuthorityCommand,
    SessionListCommand,
    SessionRenameCommand,
    SessionResumeCommand,
    SessionSendMessageCommand,
    SessionSetAuthorityCommand,
)
from code_rook.core.bus.events import (
    AgentDecisionEvent,
    CoreStartedEvent,
    PlanReadyEvent,
    SessionInterruptedEvent,
)


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


# 功能：验证会话 authority 查询与更新命令使用强类型 mode 和 profile
# 设计：执行 JSON 往返并覆盖两种非默认值，固定 TUI 与 Core 的权限模式同步协议
def test_session_authority_commands_roundtrip() -> None:
    get_command = SessionGetAuthorityCommand(session_id="sess-1")
    set_command = SessionSetAuthorityCommand(
        session_id="sess-1",
        mode=RuntimeMode.PLAN,
        profile=AuthorityProfile.AUTO_REVIEW,
    )

    restored = SessionSetAuthorityCommand.model_validate_json(
        set_command.model_dump_json()
    )

    assert get_command.type == "session.get_authority"
    assert restored.type == "session.set_authority"
    assert restored.mode == RuntimeMode.PLAN
    assert restored.profile == AuthorityProfile.AUTO_REVIEW


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

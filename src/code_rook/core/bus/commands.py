from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Discriminator, Field, model_validator

from code_rook.core.artifacts import ArtifactInventoryItem, ImageArtifactInput
from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    WorkspaceTrust,
)
from code_rook.core.goal.models import GoalRecord, GoalStatus
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    ThreadRecord,
    TurnItemRecord,
    TurnRecord,
)
from code_rook.core.session.model import SessionMode, SessionStatus


class PingCommand(BaseModel):
    type: Literal["core.ping"] = "core.ping"
    client: str


class PongResult(BaseModel):
    server_version: str
    uptime_ms: int
    received_at: str  # ISO 8601
    workspace: str
    active_runs: int = Field(ge=0)


class CoreAuthenticateCommand(BaseModel):
    type: Literal["core.authenticate"] = "core.authenticate"
    token: str


class CoreAuthenticateResult(BaseModel):
    authenticated: Literal[True] = True


class CoreShutdownCommand(BaseModel):
    type: Literal["core.shutdown"] = "core.shutdown"
    reason: str = ""


class CoreShutdownResult(BaseModel):
    shutting_down: Literal[True] = True


class AgentRunCommand(BaseModel):
    type: Literal["agent.run"] = "agent.run"
    goal: str
    permission_mode: Literal["deny", "fail_fast", "allow_list"] = "fail_fast"
    allow_tools: list[str] = Field(default_factory=list)
    resume_session_id: str | None = None
    question_mode: Literal["fail_fast", "timeout", "preset"] = "fail_fast"
    question_timeout_s: float | None = Field(default=None, gt=0, le=3600)
    preset_answers: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    # 校验 headless 提问策略所需参数并拒绝无效组合
    def _validate_question_policy(self) -> AgentRunCommand:
        if self.question_mode == "timeout" and self.question_timeout_s is None:
            raise ValueError("question_timeout_s is required in timeout mode")
        if self.question_mode == "preset" and not self.preset_answers:
            raise ValueError("preset_answers are required in preset mode")
        return self


class AgentRunResult(BaseModel):
    run_id: str
    session_id: str


class GoalCreateCommand(BaseModel):
    type: Literal["goal.create"] = "goal.create"
    session_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=100_000)
    token_budget: int | None = Field(default=None, ge=1)
    constraints: list[str] = Field(default_factory=list, max_length=100)
    completion_criteria: list[str] = Field(default_factory=list, max_length=100)
    start: bool = True


class GoalCreateResult(BaseModel):
    goal: GoalRecord
    run_id: str | None = None


class GoalGetCommand(BaseModel):
    type: Literal["goal.get"] = "goal.get"
    goal_id: str = ""
    session_id: str = ""

    @model_validator(mode="after")
    # 要求 Goal 查询明确提供 ID 或 session 作用域
    def _require_selector(self) -> GoalGetCommand:
        if not self.goal_id.strip() and not self.session_id.strip():
            raise ValueError("goal_id or session_id is required")
        return self


class GoalGetResult(BaseModel):
    goal: GoalRecord | None = None


class GoalListCommand(BaseModel):
    type: Literal["goal.list"] = "goal.list"
    session_id: str = ""
    status: GoalStatus | None = None
    limit: int = Field(default=50, ge=1, le=200)


class GoalListResult(BaseModel):
    goals: list[GoalRecord] = Field(default_factory=list)


class GoalEditCommand(BaseModel):
    type: Literal["goal.edit"] = "goal.edit"
    goal_id: str = ""
    session_id: str = ""
    objective: str = Field(min_length=1, max_length=100_000)
    completion_criteria: list[str] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    # 要求 Goal 修改明确提供 ID 或 session 作用域
    def _require_selector(self) -> GoalEditCommand:
        if not self.goal_id.strip() and not self.session_id.strip():
            raise ValueError("goal_id or session_id is required")
        return self


class _GoalSelectorCommand(BaseModel):
    goal_id: str = ""
    session_id: str = ""

    @model_validator(mode="after")
    # 要求 Goal 动作明确提供 ID 或 session 作用域
    def _require_selector(self) -> Self:
        if not self.goal_id.strip() and not self.session_id.strip():
            raise ValueError("goal_id or session_id is required")
        return self


class GoalPauseCommand(_GoalSelectorCommand):
    type: Literal["goal.pause"] = "goal.pause"


class GoalResumeCommand(_GoalSelectorCommand):
    type: Literal["goal.resume"] = "goal.resume"


class GoalClearCommand(_GoalSelectorCommand):
    type: Literal["goal.clear"] = "goal.clear"


class GoalCompleteCommand(_GoalSelectorCommand):
    type: Literal["goal.complete"] = "goal.complete"
    summary: str = Field(default="", max_length=4000)


class GoalActionResult(BaseModel):
    goal: GoalRecord
    run_id: str | None = None


class RunCancelCommand(BaseModel):
    type: Literal["run.cancel"] = "run.cancel"
    run_id: str


class RunCancelResult(BaseModel):
    run_id: str
    session_id: str
    status: Literal["cancelled"] = "cancelled"


class RunSteerCommand(BaseModel):
    type: Literal["run.steer"] = "run.steer"
    run_id: str
    content: str = Field(min_length=1, max_length=10_000)


class RunSteerResult(BaseModel):
    run_id: str
    queued: Literal[True] = True


class EventSubscribeCommand(BaseModel):
    type: Literal["event.subscribe"] = "event.subscribe"
    topics: list[str]          # fnmatch 模式，如 ["step.*", "tool.*"]
    scope: str = "global"      # "global" | "run:<run_id>"；thread_id 设置时由服务端覆盖
    replay_from_run: str | None = None  # 设置则先从 events.jsonl 回放历史再接实时流
    thread_id: str | None = Field(default=None, min_length=1)
    after_seq: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    # 拒绝混用旧 run 回放与新 thread 游标语义
    def _validate_replay_source(self) -> EventSubscribeCommand:
        if self.thread_id is not None and self.replay_from_run is not None:
            raise ValueError("thread_id and replay_from_run are mutually exclusive")
        if self.thread_id is None and self.after_seq != 0:
            raise ValueError("after_seq requires thread_id")
        return self


class EventSubscribeResult(BaseModel):
    subscription_id: str
    replayed_count: int = 0
    last_seq: int | None = None


class EventReplayCommand(BaseModel):
    type: Literal["event.replay"] = "event.replay"
    thread_id: str = Field(min_length=1)
    after_seq: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=1000)


class EventReplayResult(BaseModel):
    events: list[RuntimeEventRecord]
    latest_seq: int
    has_more: bool


class ThreadCreateCommand(BaseModel):
    type: Literal["thread.create"] = "thread.create"
    title: str = Field(default="", max_length=200)
    mode: SessionMode = "chat"


class ThreadCreateResult(BaseModel):
    thread: ThreadRecord


class ThreadListCommand(BaseModel):
    type: Literal["thread.list"] = "thread.list"
    include_archived: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class ThreadListResult(BaseModel):
    threads: list[ThreadRecord]


class ThreadGetCommand(BaseModel):
    type: Literal["thread.get"] = "thread.get"
    thread_id: str = Field(min_length=1)


class ThreadGetResult(BaseModel):
    thread: ThreadRecord


class ThreadUpdateCommand(BaseModel):
    type: Literal["thread.update"] = "thread.update"
    thread_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)


class ThreadUpdateResult(BaseModel):
    thread: ThreadRecord


class ThreadArchiveCommand(BaseModel):
    type: Literal["thread.archive"] = "thread.archive"
    thread_id: str = Field(min_length=1)


class ThreadArchiveResult(BaseModel):
    thread: ThreadRecord


class TurnStartCommand(BaseModel):
    type: Literal["turn.start"] = "turn.start"
    thread_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=100_000)
    runtime_mode: RuntimeMode = RuntimeMode.ACT


class TurnStartResult(BaseModel):
    turn_id: str


class TurnGetCommand(BaseModel):
    type: Literal["turn.get"] = "turn.get"
    turn_id: str = Field(min_length=1)


class TurnGetResult(BaseModel):
    turn: TurnRecord


class TurnListCommand(BaseModel):
    type: Literal["turn.list"] = "turn.list"
    thread_id: str = Field(min_length=1)


class TurnListResult(BaseModel):
    turns: list[TurnRecord]


class TurnInterruptCommand(BaseModel):
    type: Literal["turn.interrupt"] = "turn.interrupt"
    turn_id: str = Field(min_length=1)


class TurnInterruptResult(BaseModel):
    turn: TurnRecord


class TurnSteerCommand(BaseModel):
    type: Literal["turn.steer"] = "turn.steer"
    turn_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=10_000)


class TurnSteerResult(BaseModel):
    turn: TurnRecord


class TurnItemsCommand(BaseModel):
    type: Literal["turn.items"] = "turn.items"
    turn_id: str = Field(min_length=1)


class TurnItemsResult(BaseModel):
    items: list[TurnItemRecord]


class RuntimeCapabilitiesCommand(BaseModel):
    type: Literal["runtime.capabilities"] = "runtime.capabilities"


class RuntimeCapabilitiesResult(BaseModel):
    version: str
    runtime_modes: list[RuntimeMode]
    features: list[str]


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str
    runtime_mode: RuntimeMode = RuntimeMode.ACT
    attachments: list[ImageArtifactInput] = Field(default_factory=list, max_length=8)


class SessionSendMessageResult(BaseModel):
    run_id: str


class SessionGetAuthorityCommand(BaseModel):
    type: Literal["session.get_authority"] = "session.get_authority"
    session_id: str


class SessionSetAuthorityCommand(BaseModel):
    type: Literal["session.set_authority"] = "session.set_authority"
    session_id: str
    mode: RuntimeMode | None = None
    profile: AuthorityProfile | None = None
    workspace_trust: WorkspaceTrust | None = None

    @model_validator(mode="after")
    # 确保权限更新命令至少改变一个独立维度
    def require_change(self) -> SessionSetAuthorityCommand:
        if self.mode is None and self.profile is None and self.workspace_trust is None:
            raise ValueError("mode, profile, or workspace_trust is required")
        return self


class SessionAuthorityResult(BaseModel):
    snapshot: AuthoritySnapshot


class SessionGetHistoryCommand(BaseModel):
    type: Literal["session.get_history"] = "session.get_history"
    session_id: str


class SessionGetHistoryResult(BaseModel):
    messages: list[dict[str, Any]]


class SessionInfo(BaseModel):
    session_id: str
    mode: SessionMode
    status: SessionStatus
    title: str
    created_at: str
    updated_at: str
    run_count: int
    last_run_id: str | None = None
    parent_session_id: str | None = None
    workspace: str = ""


class SessionListCommand(BaseModel):
    type: Literal["session.list"] = "session.list"
    include_closed: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class SessionListResult(BaseModel):
    sessions: list[SessionInfo]


class SessionResumeCommand(BaseModel):
    type: Literal["session.resume"] = "session.resume"
    session_id: str


class SessionResumeResult(BaseModel):
    session: SessionInfo


class SessionRenameCommand(BaseModel):
    type: Literal["session.rename"] = "session.rename"
    session_id: str
    title: str = Field(min_length=1, max_length=200)


class SessionRenameResult(BaseModel):
    session: SessionInfo


class SessionForkCommand(BaseModel):
    type: Literal["session.fork"] = "session.fork"
    session_id: str
    title: str = Field(default="", max_length=200)


class SessionForkResult(BaseModel):
    session: SessionInfo


class SessionExportCommand(BaseModel):
    type: Literal["session.export"] = "session.export"
    session_id: str
    format: Literal["markdown", "json"] = "markdown"


class SessionExportResult(BaseModel):
    filename: str
    media_type: str
    content: str


class SessionDeleteCommand(BaseModel):
    type: Literal["session.delete"] = "session.delete"
    session_id: str


class SessionDeleteResult(BaseModel):
    session_id: str
    deleted: Literal[True] = True


class SessionCloseCommand(BaseModel):
    type: Literal["session.close"] = "session.close"
    session_id: str


class SessionCloseResult(BaseModel):
    status: SessionStatus


class PermissionRespondCommand(BaseModel):
    type: Literal["permission.respond"] = "permission.respond"
    tool_use_id: str
    # "allow_once" | "always_allow" | "deny_once" | "always_deny"
    decision: str
    selected_hunks: list[str] | None = Field(default=None, max_length=1000)
    patch_plan_id: str | None = Field(default=None, max_length=128)


class PermissionRespondResult(BaseModel):
    ok: bool = True


class UserQuestionRespondCommand(BaseModel):
    type: Literal["user_question.respond"] = "user_question.respond"
    question_id: str
    answer: str = Field(min_length=1, max_length=10_000)


class UserQuestionRespondResult(BaseModel):
    answered: Literal[True] = True


class SessionCompactCommand(BaseModel):
    type: Literal["session.compact"] = "session.compact"
    session_id: str
    focus: str = ""


class SessionCompactResult(BaseModel):
    summary_tokens: int
    saved_tokens: int
    original_tokens: int = 0
    compacted_tokens: int = 0
    retained_tokens: int = 0
    retained_messages: int = 0
    quality_score: float = 1.0
    summary_path: str = ""


class SessionTasksCommand(BaseModel):
    type: Literal["session.tasks"] = "session.tasks"
    session_id: str


class SessionTasksResult(BaseModel):
    run_id: str | None = None
    tasks: list[dict[str, Any]] = Field(default_factory=list)


class WorkerListCommand(BaseModel):
    type: Literal["worker.list"] = "worker.list"
    session_id: str = ""
    worker_id: str = ""
    root_goal_id: str = ""
    limit: int = Field(default=50, ge=1, le=200)


class WorkerListResult(BaseModel):
    workers: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowStartCommand(BaseModel):
    type: Literal["workflow.start"] = "workflow.start"
    source: str = Field(min_length=1, max_length=1_048_576)
    format: Literal["json", "toml"]


class WorkflowStartResult(BaseModel):
    workflow_id: str
    status: Literal["started"] = "started"


class WorkflowListCommand(BaseModel):
    type: Literal["workflow.list"] = "workflow.list"
    limit: int = Field(default=50, ge=1, le=200)


class WorkflowListResult(BaseModel):
    workflows: list[dict[str, Any]] = Field(default_factory=list)


class WorkflowGetCommand(BaseModel):
    type: Literal["workflow.get"] = "workflow.get"
    workflow_id: str = Field(min_length=1)


class WorkflowGetResult(BaseModel):
    workflow: dict[str, Any]


class WorkspaceDiffCommand(BaseModel):
    type: Literal["workspace.diff"] = "workspace.diff"
    scope: Literal["all", "staged", "unstaged"] = "all"
    path: str = "."


class WorkspaceDiffResult(BaseModel):
    payload: dict[str, Any]


class SessionCheckpointsCommand(BaseModel):
    type: Literal["session.checkpoints"] = "session.checkpoints"
    session_id: str
    run_id: str | None = None


class SessionCheckpointsResult(BaseModel):
    run_id: str | None = None
    checkpoints: list[dict[str, Any]] = Field(default_factory=list)


class SessionRewindCommand(BaseModel):
    type: Literal["session.rewind"] = "session.rewind"
    session_id: str
    checkpoint_id: str
    run_id: str | None = None


class SessionRewindResult(BaseModel):
    checkpoint_id: str
    restored: list[str] = Field(default_factory=list)
    already_restored: list[str] = Field(default_factory=list)


class SessionContextCommand(BaseModel):
    type: Literal["session.context"] = "session.context"
    session_id: str


class SessionContextResult(BaseModel):
    message_count: int
    estimated_tokens: int
    run_count: int
    last_run_id: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    working_set: list[str] = Field(default_factory=list)
    memory_count: int = Field(default=0, ge=0)
    compaction: dict[str, Any] | None = None
    tool_schema_tokens: int | None = Field(default=None, ge=0)
    system_tokens: int | None = Field(default=None, ge=0)


class TurnInspectCommand(BaseModel):
    type: Literal["turn.inspect"] = "turn.inspect"
    turn_id: str = Field(min_length=1)


class TurnInspectResult(BaseModel):
    turn: dict[str, Any]
    items: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    receipt: dict[str, Any]


class McpListCommand(BaseModel):
    type: Literal["mcp.list"] = "mcp.list"


class McpServerInfo(BaseModel):
    name: str
    transport: str
    status: str  # "connected" | "failed"
    tool_count: int
    tools: list[dict[str, Any]] = Field(default_factory=list)
    error: str = ""


class McpListResult(BaseModel):
    servers: list[McpServerInfo] = Field(default_factory=list)


class HooksListCommand(BaseModel):
    type: Literal["hooks.list"] = "hooks.list"
    limit: int = Field(default=20, ge=1, le=100)


class HookConfigInfo(BaseModel):
    id: str
    event: str
    blocking: bool
    trusted_scope: str
    on_failure: str
    command: list[str] = Field(default_factory=list)
    conditions: dict[str, str] = Field(default_factory=dict)


class HookAuditInfo(BaseModel):
    hook_id: str
    run_id: str = ""
    event: str
    status: str
    blocking: bool
    elapsed_ms: int
    blocked: bool
    reason: str
    exit_code: int | None = None
    process_usage: dict[str, Any] = Field(default_factory=dict)
    ts: str


class HooksListResult(BaseModel):
    configs: list[HookConfigInfo] = Field(default_factory=list)
    audit_events: list[HookAuditInfo] = Field(default_factory=list)


class HookRerunCommand(BaseModel):
    type: Literal["hooks.rerun"] = "hooks.rerun"
    hook_id: str = Field(min_length=1)


class HookRerunResult(BaseModel):
    hook_id: str
    executed: bool
    status: str = ""
    reason: str = ""
    ts: str = ""


class MemoryListCommand(BaseModel):
    type: Literal["memory.list"] = "memory.list"


class MemoryInfo(BaseModel):
    id: str
    name: str
    description: str
    type: str
    body: str
    source_session_id: str
    source_run_id: str
    created_at: str
    updated_at: str


class MemoryListResult(BaseModel):
    memories: list[MemoryInfo] = Field(default_factory=list)


class MemoryDeleteCommand(BaseModel):
    type: Literal["memory.delete"] = "memory.delete"
    memory_id: str = Field(min_length=1)


class MemoryDeleteResult(BaseModel):
    memory_id: str
    deleted: bool


class BackgroundGetCommand(BaseModel):
    type: Literal["background.get"] = "background.get"
    job_id: str = ""


class BackgroundJobInfo(BaseModel):
    id: str
    command: str
    session_id: str
    run_id: str
    status: str
    output: str
    is_error: bool
    created_at: str
    finished_at: str = ""
    process_usage: dict[str, Any] = Field(default_factory=dict)


class BackgroundGetResult(BaseModel):
    jobs: list[BackgroundJobInfo] = Field(default_factory=list)


class BackgroundCancelCommand(BaseModel):
    type: Literal["background.cancel"] = "background.cancel"
    job_id: str = Field(min_length=1)


class BackgroundCancelResult(BaseModel):
    job_id: str
    cancelled: bool


class WorkerCancelCommand(BaseModel):
    type: Literal["worker.cancel"] = "worker.cancel"
    worker_id: str = Field(min_length=1)


class WorkerCancelResult(BaseModel):
    worker_id: str
    status: str


class ArtifactListCommand(BaseModel):
    type: Literal["artifact.list"] = "artifact.list"
    days: int = Field(default=30, ge=0, le=3650)


class ArtifactListResult(BaseModel):
    artifacts: list[ArtifactInventoryItem] = Field(default_factory=list)
    total_bytes: int = Field(ge=0)
    reclaimable_bytes: int = Field(ge=0)


class ArtifactGcCommand(BaseModel):
    type: Literal["artifact.gc"] = "artifact.gc"
    days: int = Field(default=30, ge=0, le=3650)
    confirmed: bool = False


class ArtifactGcResult(BaseModel):
    dry_run: bool
    candidates: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    reclaimable_bytes: int = Field(ge=0)
    receipt_path: str = ""


# 根据 type 字段决定命令类型的判别联合
Command = Annotated[
    CoreAuthenticateCommand
    | CoreShutdownCommand
    | PingCommand
    | AgentRunCommand
    | GoalCreateCommand
    | GoalGetCommand
    | GoalListCommand
    | GoalEditCommand
    | GoalPauseCommand
    | GoalResumeCommand
    | GoalClearCommand
    | GoalCompleteCommand
    | RunCancelCommand
    | RunSteerCommand
    | EventSubscribeCommand
    | EventReplayCommand
    | ThreadCreateCommand
    | ThreadListCommand
    | ThreadGetCommand
    | ThreadUpdateCommand
    | ThreadArchiveCommand
    | TurnStartCommand
    | TurnGetCommand
    | TurnListCommand
    | TurnInterruptCommand
    | TurnSteerCommand
    | TurnItemsCommand
    | RuntimeCapabilitiesCommand
    | SessionCreateCommand
    | SessionSendMessageCommand
    | SessionGetAuthorityCommand
    | SessionSetAuthorityCommand
    | SessionGetHistoryCommand
    | SessionListCommand
    | SessionResumeCommand
    | SessionRenameCommand
    | SessionForkCommand
    | SessionExportCommand
    | SessionDeleteCommand
    | SessionCloseCommand
    | PermissionRespondCommand
    | UserQuestionRespondCommand
    | SessionCompactCommand
    | SessionTasksCommand
    | WorkerListCommand
    | WorkflowStartCommand
    | WorkflowListCommand
    | WorkflowGetCommand
    | WorkspaceDiffCommand
    | SessionCheckpointsCommand
    | SessionRewindCommand
    | SessionContextCommand
    | TurnInspectCommand
    | McpListCommand
    | HooksListCommand
    | HookRerunCommand
    | MemoryListCommand
    | MemoryDeleteCommand
    | BackgroundGetCommand
    | BackgroundCancelCommand
    | WorkerCancelCommand
    | ArtifactListCommand
    | ArtifactGcCommand,
    Discriminator("type"),
]

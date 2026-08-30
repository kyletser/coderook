from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, Discriminator, Field, JsonValue, model_validator

from code_rook.core.artifacts import ArtifactInventoryItem, ImageArtifactInput
from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    WorkspaceTrust,
)
from code_rook.core.compatibility import RuntimeCapabilitiesSnapshot
from code_rook.core.goal.models import GoalContinueDecision, GoalRecord, GoalStatus
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


class WebLaunchCommand(BaseModel):
    type: Literal["web.launch"] = "web.launch"


class WebLaunchResult(BaseModel):
    url: str
    expires_in_seconds: int = Field(ge=1, le=300)
    workspace: str


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
    auto_continue: bool = True
    max_auto_turns: int = Field(default=3, ge=1, le=100)
    max_wall_seconds: int = Field(default=1800, ge=1, le=86_400)
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


class GoalContinueDecisionCommand(_GoalSelectorCommand):
    type: Literal["goal.continue_decision"] = "goal.continue_decision"


class GoalActionResult(BaseModel):
    goal: GoalRecord
    run_id: str | None = None


class GoalContinueDecisionResult(BaseModel):
    goal: GoalRecord
    decision: GoalContinueDecision


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


class PlanRespondCommand(BaseModel):
    type: Literal["plan.respond"] = "plan.respond"
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    decision: Literal["approve", "revise", "cancel"]
    revision: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    # 只允许修改决定携带修订说明，避免其他决定混入无效状态
    def _validate_revision(self) -> PlanRespondCommand:
        if self.decision != "revise" and self.revision.strip():
            raise ValueError("revision is only valid for a revise decision")
        return self


class PlanRespondResult(BaseModel):
    session_id: str
    run_id: str
    decision: Literal["approve", "revise", "cancel"]
    status: Literal["resolved"] = "resolved"


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


class EventUnsubscribeCommand(BaseModel):
    type: Literal["event.unsubscribe"] = "event.unsubscribe"
    subscription_id: str = Field(min_length=1)


class EventUnsubscribeResult(BaseModel):
    subscription_id: str
    removed: bool


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
    preset_id: str = Field(default="standard", pattern=r"^[a-z][a-z0-9-]{0,63}$")


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


class RuntimeCapabilitiesResult(RuntimeCapabilitiesSnapshot):
    pass


class SessionCreateCommand(BaseModel):
    type: Literal["session.create"] = "session.create"
    mode: SessionMode = "chat"
    title: str = ""
    preset_id: str = Field(default="standard", pattern=r"^[a-z][a-z0-9-]{0,63}$")


class SessionCreateResult(BaseModel):
    session_id: str
    status: SessionStatus


class SessionSendMessageCommand(BaseModel):
    type: Literal["session.send_message"] = "session.send_message"
    session_id: str
    content: str
    display_content: str | None = None
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
    preset_id: str = "standard"
    preset_digest: str = ""


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
    preset_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{0,63}$",
    )


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
    session_id: str = Field(min_length=1)
    worker_id: str = ""
    root_goal_id: str = ""
    limit: int = Field(default=50, ge=1, le=200)


class WorkerListResult(BaseModel):
    workers: list[dict[str, Any]] = Field(default_factory=list)


class WorkerStartCommand(BaseModel):
    type: Literal["worker.start"] = "worker.start"
    session_id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=100_000)
    profile: str = Field(default="", max_length=64)
    route_id: str = Field(default="", max_length=256)
    model: str = Field(default="", max_length=256)
    read_only: bool = True
    exact_files: list[str] = Field(default_factory=list, max_length=200)
    write_roots: list[str] = Field(default_factory=list, max_length=100)
    coordination_contract: str = Field(default="", max_length=2_000)
    acceptance: list[str] = Field(default_factory=list, max_length=100)
    token_budget: int | None = Field(default=None, ge=1)
    wall_time_s: int = Field(default=900, ge=1, le=86_400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_s: float = Field(default=1.0, ge=0, le=300)
    backend: str = Field(default="builtin", pattern=r"^[a-z][a-z0-9-]{0,63}$")


class WorkerStartResult(BaseModel):
    worker_id: str
    session_id: str
    status: str
    route_id: str
    model: str
    attempt: int = Field(ge=1)
    worktree: str = ""
    read_only: bool
    backend: str = "builtin"
    backend_capabilities: dict[str, JsonValue] = Field(default_factory=dict)
    sandbox_enforcement: Literal["full", "partial", "unavailable"] = "unavailable"


class WorkerStatusCommand(BaseModel):
    type: Literal["worker.status"] = "worker.status"
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)


class WorkerStatusResult(BaseModel):
    worker: dict[str, Any]


class WorkerRetryCommand(BaseModel):
    type: Literal["worker.retry"] = "worker.retry"
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)


class WorkerRetryResult(WorkerStartResult):
    pass


class WorkerEventsCommand(BaseModel):
    type: Literal["worker.events"] = "worker.events"
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    after_cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class WorkerEventsResult(BaseModel):
    events: list[dict[str, Any]] = Field(default_factory=list)


class WorkerFollowupCommand(BaseModel):
    type: Literal["worker.followup"] = "worker.followup"
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=8_000)


class WorkerFollowupResult(BaseModel):
    worker_id: str
    status: str
    event_cursor: int = Field(ge=0)


class WorkerReviewCommand(BaseModel):
    type: Literal["worker.review"] = "worker.review"
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    approved: bool
    confirmed: bool = False
    expected_digest: str = ""


class WorkerReviewResult(BaseModel):
    worker_id: str
    handoff_status: str
    approved: bool
    applied: Literal[False] = False
    state_digest: str = ""
    preview_only: bool = False
    changed_files: list[str] = Field(default_factory=list)
    diff: str = ""
    diff_truncated: bool = False


class WorkerApplyCommand(BaseModel):
    type: Literal["worker.apply"] = "worker.apply"
    session_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool = False


class WorkerApplyResult(BaseModel):
    worker_id: str
    handoff_status: Literal["applied"] = "applied"
    changed_files: list[str] = Field(default_factory=list)
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


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


class WorkspaceStageCommand(BaseModel):
    type: Literal["workspace.stage"] = "workspace.stage"
    session_id: str = Field(min_length=1)
    paths: list[str] = Field(min_length=1, max_length=200)
    expected_digest: str = Field(min_length=64, max_length=64)
    confirmed: bool = False


class WorkspaceStageResult(BaseModel):
    payload: dict[str, Any]


class WorkspaceCommitCommand(BaseModel):
    type: Literal["workspace.commit"] = "workspace.commit"
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=200)
    expected_digest: str = Field(min_length=64, max_length=64)
    confirmed: bool = False


class WorkspaceCommitResult(BaseModel):
    commit: str
    subject: str
    files: list[str] = Field(default_factory=list)
    hooks_skipped: bool = True


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
    expected_digest: str = Field(min_length=64, max_length=64)
    confirmed: bool = False


class SessionRewindPreviewCommand(BaseModel):
    type: Literal["session.rewind_preview"] = "session.rewind_preview"
    session_id: str
    checkpoint_id: str
    run_id: str | None = None


class SessionRewindPreviewResult(BaseModel):
    checkpoint_id: str
    paths: list[str] = Field(default_factory=list)
    restorable: list[str] = Field(default_factory=list)
    already_restored: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    state_digest: str = Field(min_length=64, max_length=64)


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
    session_usage: dict[str, Any] = Field(default_factory=dict)
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
    session_id: str = ""


class HookRerunResult(BaseModel):
    hook_id: str
    executed: bool
    status: str = ""
    reason: str = ""
    ts: str = ""


class MemoryListCommand(BaseModel):
    type: Literal["memory.list"] = "memory.list"
    include_expired: bool = True


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
    pinned: bool = False
    expires_at: str | None = None
    expired: bool = False


class MemorySettingsInfo(BaseModel):
    auto_save: Literal["prompt", "off"] = "prompt"


class MemoryListResult(BaseModel):
    memories: list[MemoryInfo] = Field(default_factory=list)
    settings: MemorySettingsInfo = Field(default_factory=MemorySettingsInfo)


class MemoryAddCommand(BaseModel):
    type: Literal["memory.add"] = "memory.add"
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1_000)
    memory_type: Literal["user", "feedback", "project", "reference"] = "project"
    body: str = Field(min_length=1, max_length=100_000)
    source_session_id: str = ""


class MemoryEditCommand(BaseModel):
    type: Literal["memory.edit"] = "memory.edit"
    memory_id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)
    memory_type: Literal["user", "feedback", "project", "reference"] | None = None
    body: str | None = Field(default=None, min_length=1, max_length=100_000)

    @model_validator(mode="after")
    # 拒绝没有任何修改字段的 memory.edit 请求
    def require_change(self) -> Self:
        if all(
            value is None
            for value in (self.name, self.description, self.memory_type, self.body)
        ):
            raise ValueError("memory.edit requires at least one changed field")
        return self


class MemoryPinCommand(BaseModel):
    type: Literal["memory.pin"] = "memory.pin"
    memory_id: str = Field(min_length=1)
    pinned: bool = True


class MemoryExpireCommand(BaseModel):
    type: Literal["memory.expire"] = "memory.expire"
    memory_id: str = Field(min_length=1)
    expires_at: str | None = None


class MemoryMutationResult(BaseModel):
    memory: MemoryInfo


class MemorySettingsGetCommand(BaseModel):
    type: Literal["memory.settings.get"] = "memory.settings.get"


class MemorySettingsSetCommand(BaseModel):
    type: Literal["memory.settings.set"] = "memory.settings.set"
    auto_save: Literal["prompt", "off"]


class MemorySettingsResult(BaseModel):
    settings: MemorySettingsInfo


class MemoryDeleteCommand(BaseModel):
    type: Literal["memory.delete"] = "memory.delete"
    memory_id: str = Field(min_length=1)


class MemoryDeleteResult(BaseModel):
    memory_id: str
    deleted: bool


class BackgroundGetCommand(BaseModel):
    type: Literal["background.get"] = "background.get"
    session_id: str = Field(min_length=1)
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
    output_bytes: int = Field(default=0, ge=0)
    output_truncated: bool = False
    output_artifact: str = ""
    output_artifact_size: int = Field(default=0, ge=0)
    output_artifact_error: str = ""


class BackgroundGetResult(BaseModel):
    jobs: list[BackgroundJobInfo] = Field(default_factory=list)


class BackgroundCancelCommand(BaseModel):
    type: Literal["background.cancel"] = "background.cancel"
    session_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)


class BackgroundCancelResult(BaseModel):
    job_id: str
    cancelled: bool


class WorkerCancelCommand(BaseModel):
    type: Literal["worker.cancel"] = "worker.cancel"
    session_id: str = Field(min_length=1)
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
    | WebLaunchCommand
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
    | GoalContinueDecisionCommand
    | RunCancelCommand
    | RunSteerCommand
    | PlanRespondCommand
    | EventSubscribeCommand
    | EventUnsubscribeCommand
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
    | WorkerStartCommand
    | WorkerStatusCommand
    | WorkerRetryCommand
    | WorkerListCommand
    | WorkerEventsCommand
    | WorkerFollowupCommand
    | WorkerReviewCommand
    | WorkerApplyCommand
    | WorkflowStartCommand
    | WorkflowListCommand
    | WorkflowGetCommand
    | WorkspaceDiffCommand
    | WorkspaceStageCommand
    | WorkspaceCommitCommand
    | SessionCheckpointsCommand
    | SessionRewindPreviewCommand
    | SessionRewindCommand
    | SessionContextCommand
    | TurnInspectCommand
    | McpListCommand
    | HooksListCommand
    | HookRerunCommand
    | MemoryListCommand
    | MemoryAddCommand
    | MemoryEditCommand
    | MemoryPinCommand
    | MemoryExpireCommand
    | MemoryDeleteCommand
    | MemorySettingsGetCommand
    | MemorySettingsSetCommand
    | BackgroundGetCommand
    | BackgroundCancelCommand
    | WorkerCancelCommand
    | ArtifactListCommand
    | ArtifactGcCommand,
    Discriminator("type"),
]

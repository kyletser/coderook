from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator, Field, JsonValue

RunOutcomeStatus = Literal[
    "completed",
    "tool_use",
    "length",
    "incomplete",
    "content_filtered",
    "failed",
    "cancelled",
    "transport_error",
]
RunFailureCategory = Literal[
    "configuration",
    "credential",
    "network",
    "model",
    "tool",
    "index",
    "sandbox",
    "runtime",
    "permission",
    "verification",
    "user_cancelled",
]


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    listen_addr: str  # e.g. "127.0.0.1:7437"
    version: str


class AuditDegradedEvent(BaseModel):
    type: Literal["audit.degraded"] = "audit.degraded"
    source: str
    diagnostic_id: str
    error_type: str
    message: str = (
        "Audit persistence is degraded; mutating tools are paused until repair and restart."
    )
    ts: str


class RunStartedEvent(BaseModel):
    type: Literal["run.started"] = "run.started"
    run_id: str
    goal: str
    ledger_seq: int | None = Field(default=None, ge=1)
    ts: str  # ISO 8601


class RunPhaseChangedEvent(BaseModel):
    type: Literal["run.phase_changed"] = "run.phase_changed"
    run_id: str
    phase: Literal[
        "understanding",
        "exploring",
        "planning",
        "waiting_confirmation",
        "executing",
        "verifying",
        "reviewing",
        "completed",
        "failed",
        "interrupted",
    ]
    current: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    summary: str = ""
    ts: str


class RunFinishedEvent(BaseModel):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str  # "success" | "failed"
    reason: str | None = None  # "exceeded_max_steps" | "cancelled" | "llm_error" | ...
    steps: int
    outcome: RunOutcomeStatus | None = None
    failure_category: RunFailureCategory | None = None
    changes: list[JsonValue] | None = None
    verification: list[JsonValue] | None = None
    result_summary: str | None = None
    ledger_seq: int | None = Field(default=None, ge=1)
    ts: str


class StepStartedEvent(BaseModel):
    type: Literal["step.started"] = "step.started"
    run_id: str
    step: int
    ledger_seq: int | None = Field(default=None, ge=1)
    ts: str


class StepFinishedEvent(BaseModel):
    type: Literal["step.finished"] = "step.finished"
    run_id: str
    step: int
    ledger_seq: int | None = Field(default=None, ge=1)
    ts: str


class AgentDecisionEvent(BaseModel):
    type: Literal["agent.decision"] = "agent.decision"
    run_id: str
    step: int
    intent: Literal["inspect", "plan", "change", "execute", "delegate", "respond"]
    summary: str
    tool_names: list[str]
    has_visible_text: bool
    ts: str


class AgentStuckEvent(BaseModel):
    type: Literal["agent.stuck"] = "agent.stuck"
    run_id: str
    step: int
    tool_name: str
    signature: str
    repeat_count: int
    ts: str


class ToolCallStartedEvent(BaseModel):
    type: Literal["tool.call_started"] = "tool.call_started"
    run_id: str
    tool_use_id: str
    tool_name: str
    operation_id: str = ""
    params: dict[str, Any]
    step: int = 0
    ledger_seq: int | None = Field(default=None, ge=1)
    presentation: dict[str, Any] | None = None
    program_id: str = ""
    parent_tool_call_id: str = ""
    node_id: str = ""
    commit_order: int = 0
    ts: str


class ToolCallProgressEvent(BaseModel):
    type: Literal["tool.call_progress"] = "tool.call_progress"
    run_id: str
    tool_use_id: str
    tool_name: str
    elapsed_ms: int = Field(ge=0)
    output_tail: str = ""
    total_bytes: int = Field(default=0, ge=0)
    step: int = 0
    presentation: dict[str, Any] | None = None
    ts: str


class ToolCallFinishedEvent(BaseModel):
    type: Literal["tool.call_finished"] = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    operation_id: str = ""
    elapsed_ms: int
    output: str = ""
    process_usage: dict[str, Any] = Field(default_factory=dict)
    step: int = 0
    ledger_seq: int | None = Field(default=None, ge=1)
    presentation: dict[str, Any] | None = None
    sandbox_enforcement: Literal["full", "partial", "unavailable"] = "unavailable"
    failure_category: str | None = None
    program_id: str = ""
    parent_tool_call_id: str = ""
    node_id: str = ""
    commit_order: int = 0
    ts: str


class ToolCallFailedEvent(BaseModel):
    type: Literal["tool.call_failed"] = "tool.call_failed"
    run_id: str
    tool_use_id: str
    tool_name: str
    operation_id: str = ""
    # runtime_error | timeout | schema_error | permission_denied | permission_required | ...
    error_class: str
    error_message: str
    elapsed_ms: int
    attempt: int = 1  # 1=first attempt, 2=first retry, 3=second retry
    terminal: bool = True
    process_usage: dict[str, Any] = Field(default_factory=dict)
    step: int = 0
    ledger_seq: int | None = Field(default=None, ge=1)
    presentation: dict[str, Any] | None = None
    sandbox_enforcement: Literal["full", "partial", "unavailable"] = "unavailable"
    failure_category: str | None = None
    program_id: str = ""
    parent_tool_call_id: str = ""
    node_id: str = ""
    commit_order: int = 0
    ts: str


class LlmRequestPreparedEvent(BaseModel):
    type: Literal["llm.request_prepared"] = "llm.request_prepared"
    run_id: str
    step: int
    ledger_seq: int | None = Field(default=None, ge=1)
    request_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    preset_id: str = "standard"
    preset_digest: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    route_id: str = ""
    model: str = ""
    wire_format: str = ""
    execution_contract_digest: str = ""
    ts: str


class LlmTokenEvent(BaseModel):
    type: Literal["llm.token"] = "llm.token"
    run_id: str
    token: str
    ts: str


class LlmReasoningEvent(BaseModel):
    type: Literal["llm.reasoning"] = "llm.reasoning"
    run_id: str
    content: str
    ts: str


class LlmUsageEvent(BaseModel):
    type: Literal["llm.usage"] = "llm.usage"
    run_id: str
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    context_pct: float = 0.0
    # 计费模型名，供成本估算；旧事件可能缺省
    model: str = ""
    ts: str


class LlmModelSelectedEvent(BaseModel):
    type: Literal["llm.model_selected"] = "llm.model_selected"
    run_id: str
    model: str
    strategy: str  # "static" | "rule_based" | "cost_budget"
    ts: str


class LlmRouteSelectedEvent(BaseModel):
    type: Literal["llm.route_selected"] = "llm.route_selected"
    run_id: str
    route_id: str
    wire_format: str
    base_url_origin: str
    model: str
    credential_source: Literal["keyring", "file", "env", "missing"]
    strategy: str = "static"
    candidates: list[str] = Field(default_factory=list)
    reason: str = "active_route"
    step: int = 0
    accumulated_cost_usd: float | None = None
    cost_budget_usd: float | None = None
    temperature: float | None = None
    ts: str


class LlmRetryEvent(BaseModel):
    type: Literal["llm.retry"] = "llm.retry"
    run_id: str
    step: int
    kind: Literal["transient", "no_content"]
    attempt: int
    reason: str
    ts: str


class LogLineEvent(BaseModel):
    type: Literal["log.line"] = "log.line"
    run_id: str
    level: str  # "DEBUG" | "INFO" | "WARNING" | "ERROR"
    source: str
    message: str
    ts: str


class SessionCreatedEvent(BaseModel):
    type: Literal["session.created"] = "session.created"
    session_id: str
    mode: str
    ts: str


class SessionMessageReceivedEvent(BaseModel):
    type: Literal["session.message_received"] = "session.message_received"
    session_id: str
    content: str
    ts: str


class SessionWaitingForInputEvent(BaseModel):
    type: Literal["session.waiting_for_input"] = "session.waiting_for_input"
    session_id: str
    last_run_id: str
    ts: str


class GoalContinueDecisionEvent(BaseModel):
    type: Literal["goal.continue_decision"] = "goal.continue_decision"
    goal_id: str
    session_id: str
    run_id: str
    should_continue: bool
    reason: str
    auto_turns_used: int = Field(ge=0)
    remaining_auto_turns: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    token_budget: int | None = Field(default=None, ge=1)
    remaining_tokens: int | None = Field(default=None, ge=0)
    wall_elapsed_seconds: int = Field(ge=0)
    max_wall_seconds: int = Field(ge=1)
    paused_needs_confirmation: bool
    ts: str


class PlanReadyEvent(BaseModel):
    type: Literal["plan.ready"] = "plan.ready"
    session_id: str
    run_id: str
    request: str
    plan: str
    plan_ticket: str = ""
    ts: str


class PlanResolvedEvent(BaseModel):
    type: Literal["plan.resolved"] = "plan.resolved"
    session_id: str
    run_id: str
    decision: Literal["approve", "revise", "cancel"]
    revision: str = ""
    plan_ticket: str = ""
    ts: str


class PlanStepState(BaseModel):
    step: str
    status: Literal["pending", "in_progress", "completed"]


class PlanUpdatedEvent(BaseModel):
    type: Literal["plan.updated"] = "plan.updated"
    run_id: str
    explanation: str = ""
    plan: list[PlanStepState]
    plan_ticket: str = ""
    ts: str


class StrategyProposedEvent(BaseModel):
    type: Literal["strategy.proposed"] = "strategy.proposed"
    run_id: str
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: Literal["direct", "plan_first", "delegate"]
    summary: str
    ts: str


class StrategyResolvedEvent(BaseModel):
    type: Literal["strategy.resolved"] = "strategy.resolved"
    run_id: str
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy: Literal["direct", "plan_first", "delegate"]
    ticket: str = ""
    reason: str = ""
    ts: str


class SessionResumedEvent(BaseModel):
    type: Literal["session.resumed"] = "session.resumed"
    session_id: str
    ts: str


class SessionRenamedEvent(BaseModel):
    type: Literal["session.renamed"] = "session.renamed"
    session_id: str
    title: str
    ts: str


class SessionForkedEvent(BaseModel):
    type: Literal["session.forked"] = "session.forked"
    session_id: str
    source_session_id: str
    ts: str


class SessionDeletedEvent(BaseModel):
    type: Literal["session.deleted"] = "session.deleted"
    session_id: str
    ts: str


class SessionInterruptedEvent(BaseModel):
    type: Literal["session.interrupted"] = "session.interrupted"
    session_id: str
    last_run_id: str
    reason: str = "cancelled"
    ts: str


class SessionClosedEvent(BaseModel):
    type: Literal["session.closed"] = "session.closed"
    session_id: str
    ts: str


class ContextCompactedEvent(BaseModel):
    type: Literal["context.compacted"] = "context.compacted"
    session_id: str
    run_id: str
    original_tokens: int
    summary_tokens: int
    retained_tokens: int = 0
    retained_messages: int = 0
    compacted_tokens: int = 0
    quality_score: float = 1.0
    trigger: str = "auto"
    summary_path: str = ""
    strategy: str = "structured"
    pinned_fact_count: int = Field(default=0, ge=0)
    pinned_fact_retained: int = Field(default=0, ge=0)
    deduplicated_reads: int = Field(default=0, ge=0)
    ts: str


class ContextCompactionStartedEvent(BaseModel):
    type: Literal["context.compaction.started"] = "context.compaction.started"
    session_id: str
    run_id: str
    shadow_start_seq: int = Field(ge=1)
    shadow_end_seq: int = Field(ge=1)
    trigger: str = "auto"
    ts: str


class ContextCompactionSummaryEvent(BaseModel):
    type: Literal["context.compaction.summary"] = "context.compaction.summary"
    session_id: str
    run_id: str
    summary: str
    source_event_seqs: list[int]
    pinned_fact_count: int = Field(default=0, ge=0)
    ts: str


class ContextCompactionCommittedEvent(BaseModel):
    type: Literal["context.compaction.committed"] = "context.compaction.committed"
    session_id: str
    run_id: str
    shadowed_event_seqs: list[int]
    replacement_event_seqs: list[int]
    original_tokens: int = Field(ge=0)
    compacted_tokens: int = Field(ge=0)
    ts: str


class RecoveryAvailableEvent(BaseModel):
    type: Literal["recovery.available"] = "recovery.available"
    session_id: str
    run_id: str
    interruption_kind: str
    safe_to_resume: bool
    summary: str
    actions: list[str]
    ts: str


class RecoveryResolvedEvent(BaseModel):
    type: Literal["recovery.resolved"] = "recovery.resolved"
    session_id: str
    run_id: str
    action: str
    ts: str


class ContextPrefixFingerprintEvent(BaseModel):
    type: Literal["context.prefix_fingerprint"] = "context.prefix_fingerprint"
    run_id: str
    step: int
    digest: str
    source_hashes: dict[str, str]
    changed_sources: list[str]
    ts: str


class ContextWorkingSetEvent(BaseModel):
    type: Literal["context.working_set"] = "context.working_set"
    run_id: str
    step: int
    paths: list[str]
    ts: str


class TaskProfiledEvent(BaseModel):
    type: Literal["task.profiled"] = "task.profiled"
    run_id: str
    profile: dict[str, Any]
    profile_digest: str
    ts: str


class ContextRepositoryEvent(BaseModel):
    type: Literal["context.repository"] = "context.repository"
    run_id: str
    repository_hash: str
    budget_chars: int = Field(ge=1)
    used_chars: int = Field(ge=0)
    paths: list[str]
    selection_reasons: list[dict[str, Any]]
    cache_hits: int = Field(ge=0)
    parsed_files: int = Field(ge=0)
    ts: str


class ContextBudgetEvent(BaseModel):
    type: Literal["context.budget"] = "context.budget"
    run_id: str
    step: int
    message_tokens: int
    system_tokens: int
    tool_schema_tokens: int
    tool_count: int
    ts: str


class LspDiagnosticsEvent(BaseModel):
    type: Literal["lsp.diagnostics"] = "lsp.diagnostics"
    run_id: str
    step: int
    status: Literal["ok", "unavailable", "failed", "timeout", "truncated"]
    tool: str
    paths: list[str]
    diagnostic_count: int
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool
    error: str = ""
    ts: str


class VerificationCompletedEvent(BaseModel):
    type: Literal["verification.completed"] = "verification.completed"
    run_id: str
    step: int
    tool: str
    action: str
    verdict: Literal["pass"] = "pass"
    gate_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    paths: list[str]
    gates: list[dict[str, Any]]
    ts: str


class VerificationFailedEvent(BaseModel):
    type: Literal["verification.failed"] = "verification.failed"
    run_id: str
    step: int
    tool: str
    action: str
    verdict: Literal["fail"] = "fail"
    gate_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=1)
    failure_class: str
    paths: list[str]
    gates: list[dict[str, Any]]
    ts: str


class PermissionRequestedEvent(BaseModel):
    type: Literal["permission.requested"] = "permission.requested"
    run_id: str
    tool_use_id: str
    tool_name: str
    params: dict[str, Any]
    param_preview: str
    session_id: str
    ts: str


class PermissionGrantedEvent(BaseModel):
    type: Literal["permission.granted"] = "permission.granted"
    run_id: str
    tool_use_id: str
    # "allow_once" | "always_allow" | "auto_allow"
    decision: str
    ts: str


class PermissionDeniedEvent(BaseModel):
    type: Literal["permission.denied"] = "permission.denied"
    run_id: str
    tool_use_id: str
    # "deny_once" | "always_deny" | "auto_deny"
    decision: str
    ts: str


class UserQuestionAskedEvent(BaseModel):
    type: Literal["user_question.asked"] = "user_question.asked"
    question_id: str
    run_id: str
    session_id: str
    question: str
    header: str
    options: list[str]
    multi_select: bool = False
    ts: str


class RunSteeredEvent(BaseModel):
    type: Literal["run.steered"] = "run.steered"
    run_id: str
    session_id: str
    content: str
    ts: str


class SubagentStartedEvent(BaseModel):
    type: Literal["subagent.started"] = "subagent.started"
    run_id: str          # 子 agent run_id
    parent_run_id: str
    description: str
    ts: str


class SubagentFinishedEvent(BaseModel):
    type: Literal["subagent.finished"] = "subagent.finished"
    run_id: str
    parent_run_id: str
    status: str          # "success" | "failed"
    ts: str


class BackgroundJobStartedEvent(BaseModel):
    type: Literal["background.started"] = "background.started"
    job_id: str
    run_id: str
    session_id: str
    command: str
    ts: str


class BackgroundJobFinishedEvent(BaseModel):
    type: Literal["background.finished"] = "background.finished"
    job_id: str
    run_id: str
    session_id: str
    status: str
    output_preview: str
    process_usage: dict[str, Any] = Field(default_factory=dict)
    ts: str


class SkillInvokedEvent(BaseModel):
    type: Literal["skill.invoked"] = "skill.invoked"
    skill_name: str
    arguments: str
    run_id: str
    ts: str


class HookExecutedEvent(BaseModel):
    type: Literal["hook.executed"] = "hook.executed"
    hook_id: str
    run_id: str = ""
    event_name: str
    status: str
    blocking: bool
    on_failure: str
    elapsed_ms: int
    blocked: bool
    reason: str
    output_truncated: bool
    exit_code: int | None
    process_usage: dict[str, Any] = Field(default_factory=dict)
    ts: str


class RuntimeEventAppendedEvent(BaseModel):
    type: Literal["runtime.event"] = "runtime.event"
    thread_id: str
    turn_id: str | None
    seq: int
    event_type: str
    payload: dict[str, Any]
    ts: str


# 根据 type 字段决定事件类型的判别联合
Event = Annotated[
    CoreStartedEvent
    | AuditDegradedEvent
    | RunStartedEvent
    | RunPhaseChangedEvent
    | RunFinishedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | AgentDecisionEvent
    | AgentStuckEvent
    | ToolCallStartedEvent
    | ToolCallProgressEvent
    | ToolCallFinishedEvent
    | ToolCallFailedEvent
    | LlmRequestPreparedEvent
    | LlmTokenEvent
    | LlmReasoningEvent
    | LlmUsageEvent
    | LlmModelSelectedEvent
    | LlmRouteSelectedEvent
    | LlmRetryEvent
    | LogLineEvent
    | SessionCreatedEvent
    | SessionMessageReceivedEvent
    | SessionWaitingForInputEvent
    | GoalContinueDecisionEvent
    | PlanReadyEvent
    | PlanResolvedEvent
    | PlanUpdatedEvent
    | StrategyProposedEvent
    | StrategyResolvedEvent
    | SessionResumedEvent
    | SessionRenamedEvent
    | SessionForkedEvent
    | SessionDeletedEvent
    | SessionInterruptedEvent
    | SessionClosedEvent
    | ContextCompactedEvent
    | ContextCompactionStartedEvent
    | ContextCompactionSummaryEvent
    | ContextCompactionCommittedEvent
    | RecoveryAvailableEvent
    | RecoveryResolvedEvent
    | TaskProfiledEvent
    | ContextPrefixFingerprintEvent
    | ContextWorkingSetEvent
    | ContextRepositoryEvent
    | ContextBudgetEvent
    | LspDiagnosticsEvent
    | VerificationCompletedEvent
    | VerificationFailedEvent
    | PermissionRequestedEvent
    | PermissionGrantedEvent
    | PermissionDeniedEvent
    | UserQuestionAskedEvent
    | RunSteeredEvent
    | SubagentStartedEvent
    | SubagentFinishedEvent
    | BackgroundJobStartedEvent
    | BackgroundJobFinishedEvent
    | SkillInvokedEvent
    | HookExecutedEvent
    | RuntimeEventAppendedEvent,
    Discriminator("type"),
]

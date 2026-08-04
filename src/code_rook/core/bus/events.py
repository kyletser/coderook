from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Discriminator


class CoreStartedEvent(BaseModel):
    type: Literal["core.started"] = "core.started"
    listen_addr: str  # e.g. "127.0.0.1:7437"
    version: str


class RunStartedEvent(BaseModel):
    type: Literal["run.started"] = "run.started"
    run_id: str
    goal: str
    ts: str  # ISO 8601


class RunFinishedEvent(BaseModel):
    type: Literal["run.finished"] = "run.finished"
    run_id: str
    status: str  # "success" | "failed"
    reason: str | None = None  # "exceeded_max_steps" | "cancelled" | "llm_error" | ...
    steps: int
    ts: str


class StepStartedEvent(BaseModel):
    type: Literal["step.started"] = "step.started"
    run_id: str
    step: int
    ts: str


class StepFinishedEvent(BaseModel):
    type: Literal["step.finished"] = "step.finished"
    run_id: str
    step: int
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
    params: dict[str, Any]
    ts: str


class ToolCallFinishedEvent(BaseModel):
    type: Literal["tool.call_finished"] = "tool.call_finished"
    run_id: str
    tool_use_id: str
    tool_name: str
    elapsed_ms: int
    output: str = ""
    ts: str


class ToolCallFailedEvent(BaseModel):
    type: Literal["tool.call_failed"] = "tool.call_failed"
    run_id: str
    tool_use_id: str
    tool_name: str
    # runtime_error | timeout | schema_error | permission_denied | permission_required | ...
    error_class: str
    error_message: str
    elapsed_ms: int
    attempt: int = 1  # 1=first attempt, 2=first retry, 3=second retry
    terminal: bool = True
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


class PlanReadyEvent(BaseModel):
    type: Literal["plan.ready"] = "plan.ready"
    session_id: str
    run_id: str
    request: str
    plan: str
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
    truncated: bool
    error: str = ""
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
    event_name: str
    status: str
    blocking: bool
    on_failure: str
    elapsed_ms: int
    blocked: bool
    reason: str
    output_truncated: bool
    exit_code: int | None
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
    | RunStartedEvent
    | RunFinishedEvent
    | StepStartedEvent
    | StepFinishedEvent
    | AgentDecisionEvent
    | AgentStuckEvent
    | ToolCallStartedEvent
    | ToolCallFinishedEvent
    | ToolCallFailedEvent
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
    | PlanReadyEvent
    | SessionResumedEvent
    | SessionRenamedEvent
    | SessionForkedEvent
    | SessionDeletedEvent
    | SessionInterruptedEvent
    | SessionClosedEvent
    | ContextCompactedEvent
    | ContextPrefixFingerprintEvent
    | ContextWorkingSetEvent
    | ContextBudgetEvent
    | LspDiagnosticsEvent
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

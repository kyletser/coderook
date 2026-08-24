from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from code_rook.core.agents.loader import AgentProfile, AgentProfileLoader
from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    ToolAction,
    WorkspaceTrust,
    narrow_child_authority,
)
from code_rook.core.bus.events import (
    LlmRouteSelectedEvent,
    LlmUsageEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    VerificationCompletedEvent,
    VerificationFailedEvent,
)
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.events.writer import EventWriter
from code_rook.core.goal import GoalBudgetProvider
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.loop import AgentLoop
from code_rook.core.lsp import WorkspaceDiagnosticsClient
from code_rook.core.prompt_context import build_capability_context, build_runtime_context
from code_rook.core.runs import new_run_id
from code_rook.core.sandbox.planner import SandboxTier, plan_sandbox, tier_for_auto_review
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.subagent.models import WorkerRecord, WorkerStatus, WriteClaim
from code_rook.core.subagent.registry import (
    BackgroundTaskRegistry,
    WorkerConflictError,
)
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.artifact import ArtifactReadTool
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.apply_patch import ApplyPatchTool
from code_rook.core.tools.builtin.bash import BashTool
from code_rook.core.tools.builtin.checkpoint import (
    CheckpointListTool,
    CheckpointRewindTool,
)
from code_rook.core.tools.builtin.edit_file import EditFileTool
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.tools.builtin.glob import GlobTool
from code_rook.core.tools.builtin.grep import GrepTool
from code_rook.core.tools.builtin.list_dir import ListDirTool
from code_rook.core.tools.builtin.read_file import ReadFileTool
from code_rook.core.tools.builtin.skill import SkillTool
from code_rook.core.tools.builtin.task_claim import TaskClaimTool
from code_rook.core.tools.builtin.task_create import TaskCreateTool
from code_rook.core.tools.builtin.task_get import TaskGetTool
from code_rook.core.tools.builtin.task_list import TaskListTool
from code_rook.core.tools.builtin.task_update import TaskUpdateTool
from code_rook.core.tools.builtin.write_file import WriteFileTool
from code_rook.core.tools.discovery import ToolSearchTool
from code_rook.core.tools.families import (
    register_bash_family,
    register_file_family,
    register_git_family,
    register_run_family,
)
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ToolCaller
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.core.worktree import WorktreeError, WorktreeManager

if TYPE_CHECKING:
    from code_rook.core.llm.base import LLMProvider
    from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
    from code_rook.core.permissions.manager import PermissionManager
    from code_rook.core.task.manager import TaskManager

# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


_RESULT_SECTIONS = ("SUMMARY", "CHANGES", "EVIDENCE", "RISKS", "BLOCKERS")
_EVENT_TYPES = frozenset(
    {
        "agent.decision",
        "agent.stuck",
        "llm.reasoning",
        "llm.usage",
        "tool.call_started",
        "tool.call_finished",
        "tool.call_failed",
    }
)


class ParsedWorkerResult(TypedDict):
    summary: str
    changes: list[str]
    evidence: list[str]
    risks: list[str]
    blockers: list[str]


@dataclass
class _WorkerVerificationTracker:
    run_id: str
    completed: list[dict[str, Any]] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)

    # 仅接收 daemon 工具管线产生的类型化验证事件并保存有界脱敏证据
    def record(self, event: BaseModel) -> None:
        if getattr(event, "run_id", "") != self.run_id:
            return
        if not isinstance(event, (VerificationCompletedEvent, VerificationFailedEvent)):
            return
        entry = {
            "step": event.step,
            "tool": event.tool[:80],
            "action": event.action[:80],
            "verdict": event.verdict,
            "gate_count": event.gate_count,
            "passed": event.passed,
            "failed": event.failed,
            "paths": event.paths[:100],
            "gates": event.gates[:20],
            "ts": event.ts,
        }
        target = self.failed if isinstance(event, VerificationFailedEvent) else self.completed
        if len(target) < 20:
            target.append(entry)

    # 按失败优先规则返回不可由模型自报升级的验证终态
    def status(self) -> str:
        if self.failed:
            return "failed"
        if self.completed:
            return "verified"
        return "not_reported"

    # 返回可持久化到 Worker receipt 的脱敏验证事件引用
    def receipt(self) -> dict[str, Any]:
        return {
            "verification": {
                "source": "daemon_tool_events",
                "status": self.status(),
                "completed": list(self.completed),
                "failed": list(self.failed),
            }
        }


# 将子 Agent 最终文本解析为固定五段且长度受限的结果契约
def parse_worker_result(text: str) -> ParsedWorkerResult:
    sections: dict[str, list[str]] = {name: [] for name in _RESULT_SECTIONS}
    current = "SUMMARY"
    matched = False
    heading = re.compile(
        r"^\s{0,3}(?:#{1,6}\s*)?"
        r"(SUMMARY|CHANGES|EVIDENCE|RISKS|BLOCKERS)\s*:?[ \t]*(.*)$",
        re.IGNORECASE,
    )
    for line in text.splitlines():
        match = heading.match(line)
        if match:
            current = match.group(1).upper()
            matched = True
            inline = match.group(2).strip()
            if inline:
                sections[current].append(inline)
            continue
        sections[current].append(line)
    if not matched:
        sections = {name: [] for name in _RESULT_SECTIONS}
        sections["SUMMARY"] = text.splitlines()

    # 将列表段落清理为有界条目，避免把完整 transcript 返回父上下文
    def _items(name: str) -> list[str]:
        values = []
        for line in sections[name]:
            clean = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", line).strip()
            if clean:
                values.append(clean[:500])
            if len(values) >= 20:
                break
        return values

    summary = "\n".join(line.strip() for line in sections["SUMMARY"] if line.strip())
    return {
        "summary": summary[:4_000],
        "changes": _items("CHANGES"),
        "evidence": _items("EVIDENCE"),
        "risks": _items("RISKS"),
        "blockers": _items("BLOCKERS"),
    }


# 将 WorkerRecord 序列化为可注入父上下文的有界结构化摘要
def worker_result_payload(worker: WorkerRecord) -> str:
    payload = {
        "worker_id": worker.id,
        "status": worker.status.value,
        "status_reason": worker.status_reason,
        "summary": worker.summary[:4_000],
        "changes": worker.changes[:20],
        "evidence": worker.evidence[:20],
        "risks": worker.risks[:20],
        "blockers": worker.blockers[:20],
        "event_cursor": worker.event_cursor,
        "artifact_handles": worker.artifact_handles[:20],
        "token_usage": worker.token_usage,
        "input_tokens": worker.input_tokens,
        "output_tokens": worker.output_tokens,
        "cache_read_input_tokens": worker.cache_read_input_tokens,
        "cache_creation_input_tokens": worker.cache_creation_input_tokens,
        "estimated_cost_usd": worker.estimated_cost_usd,
        "cost_status": worker.cost_status,
        "worktree": worker.worktree,
        "branch": worker.branch,
        "base_commit": worker.base_commit,
        "handoff_status": worker.handoff_status,
        "changed_files": worker.changed_files[:100],
        "diff_stat": worker.diff_stat[:4_000],
        "diff_preview": worker.diff_preview[:20_000],
        "diff_truncated": worker.diff_truncated,
        "verification_status": worker.verification_status,
        "approved": worker.approved,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# 预算耗尽时生成的收尾 SUMMARY：标注 budget_exhausted，交回父上下文的结论性回执
def synthesize_budget_summary(status: str, reason: str, token_usage: int) -> str:
    return (
        f"[budget_exhausted] Worker stopped because {reason} after consuming "
        f"{token_usage} tokens. The task was not completed; use a larger token_budget "
        f"or reduce scope below. status={status}"
    )


# 从任意子运行事件提取不含参数和工具输出的简短进度摘要
def _bounded_event_summary(event: BaseModel) -> str:
    payload = event.model_dump(mode="json")
    kind = str(payload.get("type", "event"))
    for key in ("summary", "content", "tool_name", "reason", "status", "model"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"{kind}: {value.strip()[:400]}"
    return kind


# 删除子事件中的工具参数、输出和过长推理后再桥接给父 TUI
def _sanitized_parent_event(event: BaseModel) -> BaseModel | None:
    kind = str(getattr(event, "type", ""))
    if kind not in _EVENT_TYPES:
        return None
    updates: dict[str, object] = {}
    if kind == "tool.call_started":
        updates["params"] = {}
    elif kind == "tool.call_finished":
        updates["output"] = ""
    elif kind == "tool.call_failed":
        updates["error_message"] = str(getattr(event, "error_message", ""))[:500]
    elif kind == "llm.reasoning":
        updates["content"] = str(getattr(event, "content", ""))[:500]
    elif kind == "agent.decision":
        updates["summary"] = str(getattr(event, "summary", ""))[:500]
    return event.model_copy(update=updates)


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""
    worktree: str = ""
    read_only: bool = True
    exact_files: list[str] = Field(default_factory=list)
    write_roots: list[str] = Field(default_factory=list)
    coordination_contract: str = ""
    merge_owner: str = ""
    merge_reviewer: str = ""
    dependencies: list[str] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)
    token_budget: int | None = Field(default=None, ge=1)
    wall_time_s: int = Field(default=900, ge=1, le=86_400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_backoff_s: float = Field(default=1.0, ge=0, le=300)
    worker_id: str = ""


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": "When true, returns immediately with a run_id; use agent_result to poll.",  # noqa: E501
            },
            "subagent_type": {
                "type": "string",
                "description": "Agent role profile (planner/executor/reviewer). Leave empty for default.",  # noqa: E501
            },
            "worktree": {
                "type": "string",
                "description": (
                    "Optional managed worktree name from worktree_create. "
                    "All child file and bash tools are confined to that worktree."
                ),
            },
            "read_only": {
                "type": "boolean",
                "description": (
                    "True for analysis-only workers; false requires an explicit write claim."
                ),
            },
            "exact_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Exact files this worker may modify.",
            },
            "write_roots": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Directory roots this worker may modify.",
            },
            "coordination_contract": {
                "type": "string",
                "description": "Explicit coordination contract when path claims are not practical.",
            },
            "merge_owner": {"type": "string"},
            "merge_reviewer": {"type": "string"},
            "dependencies": {"type": "array", "items": {"type": "string"}},
            "acceptance": {"type": "array", "items": {"type": "string"}},
            "token_budget": {"type": "integer", "minimum": 1},
            "wall_time_s": {"type": "integer", "minimum": 1, "maximum": 86400},
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
            "retry_backoff_s": {"type": "number", "minimum": 0, "maximum": 300},
            "worker_id": {
                "type": "string",
                "description": "Interrupted or failed worker to resume/retry as a new attempt.",
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool；depth=0 表示根 agent，最大允许嵌套深度为 2
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        runs_dir: Path,
        session_id: str,
        depth: int = 0,
        workspace_boundary: WorkspaceBoundary | None = None,
        task_manager: TaskManager | None = None,
        route_registry: RouteRegistry | None = None,
        hooks: HookManager | None = None,
        max_depth: int = 3,
        interaction_manager: InteractionManager | None = None,
        authority_ceiling: AuthoritySnapshot | None = None,
        route_binding: ResolvedRoute | None = None,
        forced_worker_id: str = "",
        root_goal_id: str = "",
        require_route_readiness: bool = False,
    ) -> None:
        if not 1 <= max_depth <= 8:
            raise ValueError("max_depth must be between 1 and 8")
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._task_registry = task_registry
        self._runs_dir = runs_dir
        self._session_id = session_id
        self._depth = depth
        self._workspace_boundary = workspace_boundary or WorkspaceBoundary.current()
        self._task_manager = task_manager
        self._route_registry = route_registry
        self._hooks = hooks
        self._max_depth = max_depth
        self._interaction_manager = interaction_manager
        self._route_binding = route_binding
        self._forced_worker_id = forced_worker_id
        self._root_goal_id = root_goal_id
        self._require_route_readiness = require_route_readiness
        self._frozen_authority = authority_ceiling or (
            permission_manager.get_effective_authority_snapshot(session_id)
            if permission_manager is not None and session_id
            else AuthoritySnapshot()
        )
        self._profile_loader = AgentProfileLoader(self._workspace_boundary.root)
        self._skill_loader = SkillLoader(self._workspace_boundary.root)
        workspace_trusted = (
            self._frozen_authority.workspace_trust == WorkspaceTrust.TRUSTED
        )
        profiles = self._profile_loader.list_for_execution(
            workspace_trusted=workspace_trusted
        )
        self._profile_digests = {profile.name: profile.digest for profile in profiles}
        profile_names = [profile.name for profile in profiles]
        profile_descriptions = "; ".join(
            f"{profile.name}: {' '.join(profile.description.split())}"
            for profile in profiles
        )
        if profile_descriptions:
            self.description += f" Available profiles: {profile_descriptions}"
        subagent_schema: dict[str, object] = {
            "type": "string",
            "description": (
                "Agent role profile selected by matching its description. "
                "Leave empty for the general-purpose default."
            ),
        }
        if profile_names:
            subagent_schema["enum"] = ["", *profile_names]
        instance_schema = copy.deepcopy(type(self).input_schema)
        properties = instance_schema["properties"]
        if isinstance(properties, dict):
            properties["subagent_type"] = subagent_schema
        self.input_schema = instance_schema

    # 派生子 agent，前台时阻塞直到完成并返回结果，后台时立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)

        if self._depth >= self._max_depth:
            return ToolResult(
                content=(
                    f"Subagent nesting limit ({self._max_depth}) reached; "
                    "cannot spawn further subagents."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        retry_worker = self._task_registry.record(p.worker_id) if p.worker_id else None
        if p.worker_id and retry_worker is None:
            return ToolResult(
                content=f"Unknown worker_id: {p.worker_id}",
                is_error=True,
                error_type="runtime_error",
            )
        if retry_worker is not None:
            if retry_worker.session_id and retry_worker.session_id != self._session_id:
                return ToolResult(
                    content="Worker belongs to a different session.",
                    is_error=True,
                    error_type="conflict",
                )
            p = p.model_copy(
                update={
                    "description": retry_worker.description,
                    "prompt": retry_worker.prompt,
                    "subagent_type": retry_worker.profile,
                    "worktree": retry_worker.worktree,
                    "read_only": retry_worker.write_claim.read_only,
                    "exact_files": list(retry_worker.write_claim.exact_files),
                    "write_roots": list(retry_worker.write_claim.write_roots),
                    "coordination_contract": retry_worker.write_claim.coordination_contract,
                    "merge_owner": retry_worker.merge_owner,
                    "merge_reviewer": retry_worker.merge_reviewer,
                    "dependencies": list(retry_worker.dependencies),
                    "acceptance": list(retry_worker.acceptance),
                    "token_budget": retry_worker.token_budget,
                    "wall_time_s": retry_worker.wall_time_s,
                    "max_attempts": retry_worker.max_attempts,
                    "retry_backoff_s": retry_worker.retry_backoff_s,
                }
            )

        profile: AgentProfile | None = None
        if p.subagent_type:
            expected_digest = self._profile_digests.get(p.subagent_type)
            if expected_digest is None:
                return ToolResult(
                    content=(
                        "Unknown subagent profile or unavailable in this trust context: "
                        f"{p.subagent_type}"
                    ),
                    is_error=True,
                    error_type="runtime_error",
                )
            try:
                profile = self._profile_loader.load_for_execution(
                    p.subagent_type,
                    workspace_trusted=(
                        self._frozen_authority.workspace_trust
                        == WorkspaceTrust.TRUSTED
                    ),
                    expected_digest=expected_digest,
                )
            except ValueError as exc:
                return ToolResult(
                    content=str(exc),
                    is_error=True,
                    error_type="integrity_error",
                )
            if profile is None:
                return ToolResult(
                    content=f"Unknown subagent profile: {p.subagent_type}",
                    is_error=True,
                    error_type="runtime_error",
                )
            if (
                retry_worker is not None
                and retry_worker.profile_digest
                and retry_worker.profile_digest != profile.digest
            ):
                return ToolResult(
                    content="Agent profile changed since worker creation.",
                    is_error=True,
                    error_type="integrity_error",
                )
            if (
                retry_worker is not None
                and self._require_route_readiness
                and not retry_worker.profile_digest
            ):
                return ToolResult(
                    content="Legacy worker has no immutable profile receipt.",
                    is_error=True,
                    error_type="integrity_error",
                )

        effective_read_only = p.read_only or bool(
            profile and profile.restrict == "read_only"
        )

        parent_worker = self._task_registry.record(self._parent_run_id)
        root_goal_id = (
            parent_worker.root_goal_id
            if parent_worker
            else self._root_goal_id or self._parent_run_id
        )
        inherited_budget = (
            parent_worker.token_budget if parent_worker else p.token_budget
        )
        parent_authority = (
            parent_worker.authority_ceiling
            if parent_worker is not None
            else self._frozen_authority
        )
        allowed_actions = parent_authority.allowed_actions
        profile_ceiling = parent_authority.profile
        requested_mode = RuntimeMode.PLAN if effective_read_only else None
        requested_trust = None
        if retry_worker is not None:
            allowed_actions &= retry_worker.authority_ceiling.allowed_actions
            profile_ceiling = retry_worker.authority_ceiling.profile
            requested_mode = retry_worker.authority_ceiling.mode
            requested_trust = retry_worker.authority_ceiling.workspace_trust
        if effective_read_only:
            allowed_actions &= frozenset({ToolAction.READ})
        child_authority = narrow_child_authority(
            parent_authority,
            profile_ceiling=profile_ceiling,
            allowed_actions=allowed_actions,
            requested_mode=requested_mode,
            requested_trust=requested_trust,
        )
        effective_read_only = effective_read_only or (
            child_authority.allowed_actions <= frozenset({ToolAction.READ})
        )
        try:
            write_claim = WriteClaim(
                read_only=effective_read_only,
                exact_files=p.exact_files,
                write_roots=p.write_roots,
                coordination_contract=p.coordination_contract,
            )
        except ValidationError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")

        child_run_id = p.worker_id or self._forced_worker_id or new_run_id()
        worktree_name = p.worktree
        base_commit = retry_worker.base_commit if retry_worker is not None else ""
        merge_owner = p.merge_owner
        merge_reviewer = p.merge_reviewer
        worktree_manager = WorktreeManager(self._workspace_boundary.root)
        auto_created_worktree = False

        # 回滚启动失败前自动创建的 worktree，已有或重试 worktree 永不由此路径删除
        async def _cleanup_auto_worktree() -> None:
            if not auto_created_worktree or not worktree_name:
                return
            await worktree_manager.cleanup_failed_creation(worktree_name)

        if not effective_read_only:
            if retry_worker is not None and not worktree_name:
                return ToolResult(
                    content="Legacy writing worker has no isolated worktree and cannot be retried.",
                    is_error=True,
                    error_type="conflict",
                )
            if not worktree_name:
                worktree_name = child_run_id
                try:
                    base_commit = await worktree_manager.resolve_ref("HEAD")
                    await worktree_manager.create(worktree_name, base_commit)
                    auto_created_worktree = True
                except WorktreeError as exc:
                    return ToolResult(
                        content=f"Unable to create isolated worker worktree: {exc}",
                        is_error=True,
                        error_type="runtime_error",
                    )
            else:
                if retry_worker is None and any(
                    item.worktree == worktree_name
                    for item in self._task_registry.list_records()
                ):
                    return ToolResult(
                        content=f"managed worktree already belongs to a worker: {worktree_name}",
                        is_error=True,
                        error_type="conflict",
                    )
                try:
                    if not base_commit:
                        base_commit = await worktree_manager.resolve_ref("HEAD")
                    managed_path = worktree_manager.path_for(worktree_name)
                except WorktreeError as exc:
                    return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
                if not managed_path.is_dir():
                    return ToolResult(
                        content=f"managed worktree not found: {worktree_name}",
                        is_error=True,
                        error_type="runtime_error",
                    )
            merge_owner = merge_owner.strip() or f"parent:{self._parent_run_id}"
            merge_reviewer = merge_reviewer.strip() or "user"
        child_capabilities = build_capability_context(
            self._skill_loader.list_for_execution(
                workspace_trusted=(
                    child_authority.workspace_trust == WorkspaceTrust.TRUSTED
                )
            ),
            self._profile_loader.list_for_execution(
                workspace_trusted=(
                    child_authority.workspace_trust == WorkspaceTrust.TRUSTED
                )
            ),
        )
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            runtime_context=build_runtime_context(self._workspace_boundary.root),
            capability_context=child_capabilities,
            system_prompt_override=profile.system_prompt if profile else None,
        )
        child_context.runtime_mode = child_authority.mode
        result_contract = (
            "Return only a concise structured handoff using these exact headings: "
            "SUMMARY, CHANGES, EVIDENCE, RISKS, BLOCKERS. Do not include the full transcript."
        )
        child_context.project_context = result_contract

        child_boundary = self._workspace_boundary
        if worktree_name:
            try:
                worktree_path = worktree_manager.path_for(worktree_name)
            except WorktreeError as exc:
                await _cleanup_auto_worktree()
                return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
            if not worktree_path.is_dir():
                await _cleanup_auto_worktree()
                return ToolResult(
                    content=f"managed worktree not found: {worktree_name}",
                    is_error=True,
                    error_type="runtime_error",
                )
            child_boundary = WorkspaceBoundary(worktree_path)
            child_context.project_context = (
                f"You are isolated in Git worktree '{worktree_name}' at {worktree_path}. "
                "All file and shell operations must remain in this worktree. "
                + result_contract
            )
            child_context.runtime_context = build_runtime_context(child_boundary.root)
            child_context.capability_context = build_capability_context(
                SkillLoader(child_boundary.root).list_for_execution(
                    workspace_trusted=(
                        child_authority.workspace_trust == WorkspaceTrust.TRUSTED
                    )
                ),
                AgentProfileLoader(child_boundary.root).list_for_execution(
                    workspace_trusted=(
                        child_authority.workspace_trust == WorkspaceTrust.TRUSTED
                    )
                ),
            )

        child_bus = EventBus()
        verification_tracker = _WorkerVerificationTracker(child_run_id)

        # 持久化有界子事件并只把脱敏进度桥接到父 TUI
        async def _bridge(event: BaseModel) -> None:
            verification_tracker.record(event)
            if self._task_registry.record(child_run_id) is not None:
                self._task_registry.append_event(
                    child_run_id,
                    str(getattr(event, "type", "event")),
                    _bounded_event_summary(event),
                )
                if isinstance(event, LlmUsageEvent):
                    exhausted = self._task_registry.add_llm_usage(
                        child_run_id,
                        input_tokens=event.input_tokens,
                        output_tokens=event.output_tokens,
                        cache_read_input_tokens=event.cache_read_input_tokens,
                        cache_creation_input_tokens=event.cache_creation_input_tokens,
                        model=event.model,
                    )
                    if exhausted and not child_context.is_done():
                        child_context.status = WorkerStatus.BUDGET_LIMITED.value
                        child_context.reason = "root goal token budget exhausted"
            sanitized = _sanitized_parent_event(event)
            if sanitized is not None:
                await self._parent_bus.publish(sanitized)

        child_bus.subscribe(_bridge)

        # 同 run 内所有 child 共享 TaskManager，或 fallback 新建独立实例
        task_manager = self._task_manager or TaskManager(
            self._runs_dir / child_run_id / ".tasks"
        )
        child_provider = self._provider
        route_event: LlmRouteSelectedEvent | None = None
        effective_route = self._route_binding
        if profile and (profile.route or profile.model) and self._route_binding is not None:
            from code_rook.core.llm.factory import create_provider_for_route
            from code_rook.core.llm.route_registry import (
                ResolvedRoute,
                RouteResolutionError,
            )

            if self._route_registry is None:
                await _cleanup_auto_worktree()
                return ToolResult(
                    content="Profile route registry is unavailable.",
                    is_error=True,
                    error_type="runtime_error",
                )
            target_route_id = profile.route or self._route_binding.route.id
            target_model = profile.model or None
            try:
                if self._require_route_readiness:
                    effective_route = await self._route_registry.resolve_ready(
                        target_route_id,
                        model=target_model,
                    )
                else:
                    selected = self._route_registry.resolve(target_route_id)
                    pinned = selected.route
                    if target_model is not None and target_model != pinned.model:
                        pinned = pinned.model_copy(update={"model": target_model})
                    effective_route = ResolvedRoute(
                        route=pinned,
                        receipt=pinned.receipt(selected.receipt.credential_source),
                        credential=selected.credential,
                    )
            except RouteResolutionError as exc:
                await _cleanup_auto_worktree()
                return ToolResult(
                    content=str(exc),
                    is_error=True,
                    error_type="runtime_error",
                )
            try:
                child_provider = create_provider_for_route(
                    effective_route.route,
                    effective_route.credential,
                )
            except Exception as exc:
                await _cleanup_auto_worktree()
                return ToolResult(
                    content=f"Unable to create profile provider: {exc}",
                    is_error=True,
                    error_type="runtime_error",
                )
            if isinstance(self._provider, GoalBudgetProvider):
                child_provider = self._provider.wrap(child_provider)
        elif profile and profile.route:
            from code_rook.core.llm.factory import create_provider_for_route
            from code_rook.core.llm.route_registry import ResolvedRoute

            if self._route_registry is None:
                await _cleanup_auto_worktree()
                return ToolResult(
                    content="Profile route registry is unavailable.",
                    is_error=True,
                    error_type="runtime_error",
                )
            try:
                if self._require_route_readiness:
                    effective_route = await self._route_registry.resolve_ready(
                        profile.route,
                        model=profile.model or None,
                    )
                else:
                    selected = self._route_registry.resolve(profile.route)
                    pinned = selected.route
                    if profile.model and profile.model != pinned.model:
                        pinned = pinned.model_copy(update={"model": profile.model})
                    effective_route = ResolvedRoute(
                        route=pinned,
                        receipt=pinned.receipt(selected.receipt.credential_source),
                        credential=selected.credential,
                    )
                child_provider = create_provider_for_route(
                    effective_route.route,
                    effective_route.credential,
                )
            except Exception as exc:
                await _cleanup_auto_worktree()
                return ToolResult(
                    content=f"Unable to create profile provider: {exc}",
                    is_error=True,
                    error_type="runtime_error",
                )
            if isinstance(self._provider, GoalBudgetProvider):
                child_provider = self._provider.wrap(child_provider)
        elif profile and profile.model:
            _parent_provider = self._provider
            _child_model = profile.model

            class _ModelOverrideProvider:
                # 将 profile 模型覆盖注入父 provider，保持其显式 wire format 不变
                async def chat(self, *a: Any, **kw: Any) -> Any:
                    return await _parent_provider.chat(*a, model=_child_model, **kw)

            child_provider = _ModelOverrideProvider()

        worker_route = effective_route.route.id if effective_route is not None else ""
        worker_model = (
            effective_route.route.model
            if effective_route is not None
            else profile.model if profile else ""
        )
        worker_route_digest = (
            effective_route.route.validation_digest()
            if effective_route is not None
            else ""
        )
        if retry_worker is not None:
            if self._require_route_readiness and (
                not retry_worker.route or not retry_worker.route_digest
            ):
                await _cleanup_auto_worktree()
                return ToolResult(
                    content="Legacy worker has no immutable route receipt and cannot be retried.",
                    is_error=True,
                    error_type="integrity_error",
                )
            if retry_worker.route_digest and (
                retry_worker.route != worker_route
                or retry_worker.model != worker_model
                or retry_worker.route_digest != worker_route_digest
            ):
                await _cleanup_auto_worktree()
                return ToolResult(
                    content="Worker route changed since the original attempt.",
                    is_error=True,
                    error_type="integrity_error",
                )
        if effective_route is not None:
            receipt = effective_route.route.receipt(
                effective_route.receipt.credential_source
            )
            route_event = LlmRouteSelectedEvent(
                run_id=child_run_id,
                route_id=receipt.route_id,
                wire_format=receipt.wire_format,
                base_url_origin=receipt.base_url_origin,
                model=receipt.model,
                credential_source=receipt.credential_source,
                ts=_now(),
            )

        try:
            child_registry = self._build_child_registry(
                child_bus,
                child_run_id,
                profile,
                child_boundary,
                task_manager,
                child_provider,
                child_authority,
                effective_read_only,
                effective_route,
            )

            child_loop = AgentLoop(
                child_provider,
                child_registry,
                child_bus,
                permission_manager=self._permission_manager,
                session_id=self._session_id,
                todo_state=task_manager,
                artifact_store=ArtifactStore(
                    child_boundary.root / ".coderook" / "artifacts"
                ),
                diagnostics_client=WorkspaceDiagnosticsClient(child_boundary),
                hooks=self._hooks,
                interaction_manager=self._interaction_manager,
                authority_snapshot=child_authority,
            )
        except Exception as exc:
            await _cleanup_auto_worktree()
            return ToolResult(
                content=f"Unable to assemble isolated worker: {exc}",
                is_error=True,
                error_type="runtime_error",
            )

        if route_event is not None:
            await self._parent_bus.publish(route_event)
        if self._hooks is not None:
            worker_decision = await self._hooks.emit(
                "worker_started",
                {
                    "run_id": child_run_id,
                    "parent_run_id": self._parent_run_id,
                    "session_id": self._session_id,
                    "background": p.run_in_background,
                    "profile": p.subagent_type,
                },
            )
            if worker_decision.blocked:
                await _cleanup_auto_worktree()
                return ToolResult(
                    content=worker_decision.reason or "Worker start blocked by hook.",
                    is_error=True,
                    error_type="hook_denied",
                )

        worker: WorkerRecord
        try:
            if retry_worker is not None:
                worker = self._task_registry.prepare_retry(
                    retry_worker.id,
                    authority_ceiling=child_authority,
                )
            else:
                worker = self._task_registry.new_record(
                    worker_id=child_run_id,
                    parent_turn_id=self._parent_run_id,
                    parent_worker_id=parent_worker.id if parent_worker else "",
                    root_goal_id=root_goal_id,
                    session_id=self._session_id,
                    description=p.description,
                    prompt=p.prompt,
                    role=p.subagent_type or "general-purpose",
                    profile=p.subagent_type,
                    profile_digest=profile.digest if profile is not None else "",
                    route=worker_route,
                    route_digest=worker_route_digest,
                    model=worker_model,
                    reasoning=profile.reasoning if profile is not None else "",
                    workspace=str(child_boundary.root),
                    worktree=worktree_name,
                    branch=f"coderook/{worktree_name}" if worktree_name else "",
                    base_commit=base_commit,
                    merge_owner=merge_owner,
                    merge_reviewer=merge_reviewer,
                    authority_ceiling=child_authority,
                    write_claim=write_claim,
                    dependencies=p.dependencies,
                    acceptance=p.acceptance,
                    depth=self._depth + 1,
                    max_steps=self._max_steps,
                    wall_time_s=p.wall_time_s,
                    token_budget=inherited_budget,
                    max_attempts=p.max_attempts,
                    retry_backoff_s=p.retry_backoff_s,
                )
                self._task_registry.create(worker)
            if not p.run_in_background:
                worker = self._task_registry.start(worker.id)
        except (WorkerConflictError, ValueError) as exc:
            await _cleanup_auto_worktree()
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_type="conflict",
            )
        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        child_run_path = self._runs_dir / child_run_id
        child_run_path.mkdir(parents=True, exist_ok=True)

        if p.run_in_background:
            task: asyncio.Task[None] = asyncio.create_task(
                self._run_background(
                    child_loop,
                    child_context,
                    child_bus,
                    child_run_path,
                    child_run_id,
                    verification_tracker,
                )
            )
            self._task_registry.register(
                child_run_id,
                task,
                child_context,
                parent_run_id=self._parent_run_id,
            )
            return ToolResult(
                content=(
                    f"Worker started. worker_id={child_run_id}. run_id={child_run_id}. "
                    "Use Agent action=status, peek, or wait to inspect it."
                )
            )

        try:
            if self._interaction_manager is not None:
                self._interaction_manager.register_run(child_run_id)
            async with EventWriter(child_run_path / "events.jsonl") as writer:
                writer.subscribe(child_bus)
                async with asyncio.timeout(p.wall_time_s):
                    await child_loop.run(child_context)
        except TimeoutError:
            if not child_context.is_done():
                child_context.mark_failed("wall_time_exceeded")
        except asyncio.CancelledError:
            if not child_context.is_done():
                child_context.mark_failed("cancelled")
            self._task_registry.update_status(
                child_run_id,
                WorkerStatus.CANCELLED,
                reason="cancelled",
            )
            raise
        finally:
            if self._interaction_manager is not None:
                self._interaction_manager.unregister_run(child_run_id)
            if self._hooks is not None:
                await self._hooks.emit(
                    "worker_finished",
                    {
                        "run_id": child_run_id,
                        "parent_run_id": self._parent_run_id,
                        "session_id": self._session_id,
                        "status": child_context.status,
                    },
                )
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=child_run_id,
                    parent_run_id=self._parent_run_id,
                    status=child_context.status,
                    ts=_now(),
                )
            )

        parsed = parse_worker_result(child_context.result)
        handoff = await self._inspect_handoff(
            worktree_manager,
            worktree_name=worktree_name,
            base_commit=base_commit,
            write_claim=write_claim,
            verification_tracker=verification_tracker,
        )
        foreground_blockers = list(parsed["blockers"])
        handoff_error = handoff.pop("handoff_error", "")
        if handoff_error:
            foreground_blockers.append(str(handoff_error))
        terminal_status = WorkerStatus.FAILED
        if child_context.status == "success":
            terminal_status = WorkerStatus.COMPLETED
        elif child_context.status == WorkerStatus.BUDGET_LIMITED.value:
            terminal_status = WorkerStatus.BUDGET_LIMITED
        elif child_context.status == WorkerStatus.INTERRUPTED.value:
            terminal_status = WorkerStatus.INTERRUPTED
        elif child_context.reason == "cancelled":
            terminal_status = WorkerStatus.CANCELLED
        if handoff_error and str(handoff.get("handoff_status", "")).startswith(
            "blocked_"
        ):
            terminal_status = WorkerStatus.FAILED
        terminal_reason = child_context.reason or ""
        if terminal_status == WorkerStatus.FAILED and handoff_error:
            terminal_reason = terminal_reason or "handoff_blocked"
        worker = self._task_registry.update_status(
            child_run_id,
            terminal_status,
            reason=terminal_reason,
            summary=str(parsed["summary"]),
            changes=[str(item) for item in parsed["changes"]],
            evidence=[str(item) for item in parsed["evidence"]],
            risks=[str(item) for item in parsed["risks"]],
            blockers=foreground_blockers,
            **handoff,
        )
        return ToolResult(
            content=worker_result_payload(worker),
            is_error=terminal_status != WorkerStatus.COMPLETED,
            error_type=(
                "runtime_error" if terminal_status != WorkerStatus.COMPLETED else None
            ),
        )

    # 后台任务协程：写事件文件，运行 loop，发布完成事件
    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        run_id: str,
        verification_tracker: _WorkerVerificationTracker,
    ) -> None:
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(run_id))
        if self._interaction_manager is not None:
            self._interaction_manager.register_run(run_id)
        try:
            async with EventWriter(run_path / "events.jsonl") as writer:
                writer.subscribe(bus)
                worker = self._task_registry.record(run_id)
                wall_time_s = worker.wall_time_s if worker is not None else 900
                async with asyncio.timeout(wall_time_s):
                    await loop.run(context)
        except TimeoutError:
            if not context.is_done():
                context.mark_failed("wall_time_exceeded")
        except asyncio.CancelledError:
            if not context.is_done():
                context.mark_failed("cancelled")
            raise
        finally:
            if self._interaction_manager is not None:
                self._interaction_manager.unregister_run(run_id)
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            current = self._task_registry.record(run_id)
            if current is not None:
                parsed = parse_worker_result(context.result)
                status = WorkerStatus.FAILED
                reason = context.reason or ""
                if context.status == "success":
                    status = WorkerStatus.COMPLETED
                elif context.status == WorkerStatus.BUDGET_LIMITED.value:
                    status = WorkerStatus.BUDGET_LIMITED
                elif context.status == WorkerStatus.INTERRUPTED.value:
                    status = WorkerStatus.INTERRUPTED
                elif reason == "cancelled":
                    status = WorkerStatus.CANCELLED
                # 预算耗尽时结果常为空：用合成收尾回执补足 SUMMARY，避免父上下文拿到空结果
                summary_text = str(parsed["summary"])
                if status == WorkerStatus.BUDGET_LIMITED and not summary_text:
                    summary_text = synthesize_budget_summary(
                        status.value,
                        reason or "token budget exhausted",
                        current.token_usage,
                    )
                handoff = await self._inspect_handoff(
                    WorktreeManager(self._workspace_boundary.root),
                    worktree_name=current.worktree,
                    base_commit=current.base_commit,
                    write_claim=current.write_claim,
                    verification_tracker=verification_tracker,
                )
                blockers = [str(item) for item in parsed["blockers"]]
                handoff_error = handoff.pop("handoff_error", "")
                if handoff_error:
                    blockers.append(str(handoff_error))
                if handoff_error and str(
                    handoff.get("handoff_status", "")
                ).startswith("blocked_"):
                    status = WorkerStatus.FAILED
                    reason = reason or "handoff_blocked"
                self._task_registry.update_status(
                    run_id,
                    status,
                    reason=reason,
                    summary=summary_text,
                    changes=[str(item) for item in parsed["changes"]],
                    evidence=[str(item) for item in parsed["evidence"]],
                    risks=[str(item) for item in parsed["risks"]],
                    blockers=blockers,
                    **handoff,
                )
            if self._hooks is not None:
                await self._hooks.emit(
                    "worker_finished",
                    {
                        "run_id": run_id,
                        "parent_run_id": self._parent_run_id,
                        "session_id": self._session_id,
                        "status": context.status,
                    },
                )
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=run_id,
                    parent_run_id=self._parent_run_id,
                    status=context.status,
                    ts=_now(),
                )
            )

    # 收集只读或隔离 worktree 的权威 Git 证据，任何检查失败都阻断 handoff
    async def _inspect_handoff(
        self,
        manager: WorktreeManager,
        *,
        worktree_name: str,
        base_commit: str,
        write_claim: WriteClaim,
        verification_tracker: _WorkerVerificationTracker,
    ) -> dict[str, Any]:
        verification_status = verification_tracker.status()
        receipt = verification_tracker.receipt()
        if not worktree_name:
            return {
                "handoff_status": "read_only",
                "verification_status": verification_status,
                "receipt": receipt,
            }
        if not base_commit:
            return {
                "handoff_status": "blocked_inspection_failed",
                "verification_status": verification_status,
                "receipt": receipt,
                "handoff_error": "worktree handoff has no immutable base commit",
            }
        try:
            inspection = await manager.inspect(
                worktree_name,
                base_commit=base_commit,
            )
        except WorktreeError as exc:
            return {
                "handoff_status": "blocked_inspection_failed",
                "verification_status": verification_status,
                "receipt": receipt,
                "handoff_error": f"worktree handoff inspection failed: {exc}",
            }
        claim_violations = self._claim_violations(
            list(inspection.changed_files),
            write_claim,
        )
        if claim_violations:
            return {
                "handoff_status": "blocked_claim_violation",
                "changed_files": list(inspection.changed_files),
                "verification_status": verification_status,
                "receipt": receipt,
                "handoff_error": (
                    "worker changed paths outside its write claim: "
                    + ", ".join(claim_violations)
                ),
            }
        diff_preview = inspection.diff[:20_000]
        untracked = [
            path
            for path in inspection.changed_files
            if f" b/{path}" not in inspection.diff and f" a/{path}" not in inspection.diff
        ]
        if untracked:
            suffix = "\n".join(f"Untracked: {path}" for path in untracked)
            diff_preview = (diff_preview + "\n" + suffix).strip()[:20_000]
        return {
            "handoff_status": (
                "pending_review" if inspection.changed_files else "completed_without_changes"
            ),
            "changed_files": list(inspection.changed_files),
            "diff_stat": inspection.diff_stat,
            "diff_preview": diff_preview,
            "diff_truncated": inspection.diff_truncated or len(inspection.diff) > 20_000,
            "verification_status": verification_status,
            "receipt": receipt,
        }

    # 返回不在 Worker 精确文件或目录 claim 内的变更路径，路径穿越一律视为违规
    @staticmethod
    def _claim_violations(paths: list[str], claim: WriteClaim) -> list[str]:
        if claim.read_only:
            return list(paths)
        if not claim.exact_files and not claim.write_roots:
            return [] if claim.coordination_contract.strip() else list(paths)

        # 将 Git 路径和声明统一为不含绝对路径、点段或父目录穿越的 POSIX 形式
        def _normalized(value: str) -> str | None:
            raw = value.replace("\\", "/").strip("/")
            parts = tuple(part for part in raw.split("/") if part not in {"", "."})
            if not raw or value.startswith(("/", "\\")) or ".." in parts:
                return None
            return "/".join(parts)

        exact = {
            normalized
            for value in claim.exact_files
            if (normalized := _normalized(value)) is not None
        }
        roots = {
            normalized
            for value in claim.write_roots
            if (normalized := _normalized(value)) is not None
        }
        violations: list[str] = []
        for value in paths:
            normalized = _normalized(value)
            if normalized is None or not (
                normalized in exact
                or any(
                    normalized == root or normalized.startswith(root + "/")
                    for root in roots
                )
            ):
                violations.append(value)
        return violations

    # 按 WorkerRecord 配置定期刷新租约心跳，终态或任务取消时退出
    async def _heartbeat_loop(self, run_id: str) -> None:
        while True:
            worker = self._task_registry.record(run_id)
            if worker is None or worker.status not in {
                WorkerStatus.QUEUED,
                WorkerStatus.RUNNING,
                WorkerStatus.WAITING,
            }:
                return
            await asyncio.sleep(worker.heartbeat_interval_s)
            worker = self._task_registry.record(run_id)
            if worker is None or worker.status not in {
                WorkerStatus.QUEUED,
                WorkerStatus.RUNNING,
                WorkerStatus.WAITING,
            }:
                return
            self._task_registry.heartbeat(run_id)

    # 构造子 registry；基于角色配置过滤工具：显式 allowed_tools 与 restrict capability 取最严子集
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
        boundary: WorkspaceBoundary,
        task_manager: TaskManager,
        provider: LLMProvider | None = None,
        authority_ceiling: AuthoritySnapshot | None = None,
        force_read_only: bool = False,
        route_binding: ResolvedRoute | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = (
            set(profile.allowed_tools) if profile and profile.allowed_tools else None
        )
        authority_read_only = bool(
            authority_ceiling is not None
            and authority_ceiling.allowed_actions <= frozenset({ToolAction.READ})
        )
        restrict_read_only = force_read_only or authority_read_only or bool(
            profile and profile.restrict == "read_only"
        )

        # 显式名单和 restrict read-only 同时存在时取从严子集，单独存在时分别生效
        def _allowed(tool: BaseTool | str) -> bool:
            name = tool.name if isinstance(tool, BaseTool) else tool
            if restrict_read_only:
                is_read_only_t = (
                    tool.is_read_only
                    if isinstance(tool, BaseTool)
                    else name in {"agent", "agent_result"}
                )
                if not is_read_only_t:
                    return False
            return allowed is None or name in allowed

        registry = ToolRegistry(
            runtime_mode=RuntimeMode.PLAN if restrict_read_only else RuntimeMode.ACT,
            allowed_authority_actions=(
                authority_ceiling.allowed_actions
                if authority_ceiling is not None
                else None
            ),
        )
        checkpoint_store = CheckpointStore(
            self._runs_dir / child_run_id / ".checkpoints",
            boundary,
        )
        file_tools = [
            ReadFileTool(boundary),
            GlobTool(boundary),
            GrepTool(boundary),
            EditFileTool(
                boundary,
                checkpoint_store=checkpoint_store,
            ),
            ApplyPatchTool(
                boundary,
                checkpoint_store=checkpoint_store,
            ),
            WriteFileTool(
                boundary,
                checkpoint_store=checkpoint_store,
            ),
            ListDirTool(boundary),
        ]
        file_allowed = allowed
        if restrict_read_only:
            read_aliases = {tool.name for tool in file_tools if tool.is_read_only}
            file_allowed = (
                read_aliases
                if allowed is None or "File" in allowed
                else read_aliases & allowed
            )
        register_file_family(
            registry,
            boundary,
            file_tools,
            allowed_names=file_allowed,
        )
        artifact_tool = ArtifactReadTool(
            ArtifactStore(boundary.root / ".coderook" / "artifacts")
        )
        if _allowed(artifact_tool):
            registry.register(artifact_tool)
        register_git_family(
            registry,
            boundary,
            GitDiffTool(boundary),
            allowed_names=allowed,
        )
        sandbox_plan = None
        if (
            authority_ceiling is not None
            and authority_ceiling.profile == AuthorityProfile.AUTO_REVIEW
            and tier_for_auto_review(authority_ceiling.sandbox) != SandboxTier.NONE
        ):
            sandbox_plan = plan_sandbox(
                authority_ceiling.sandbox,
                SandboxTier.WORKSPACE_WRITE,
                str(boundary.root),
            )
        shell = BashTool(boundary.root, sandbox_plan=sandbox_plan)
        if not restrict_read_only:
            register_run_family(
                registry,
                shell,
                allowed_names=allowed,
                verification_boundary=boundary,
            )
            register_bash_family(
                registry,
                shell,
                allowed_names=allowed,
            )
        skill_tool = SkillTool(
            SkillLoader(boundary.root),
            workspace_trusted=(
                authority_ceiling is not None
                and authority_ceiling.workspace_trust == WorkspaceTrust.TRUSTED
            ),
        )
        if _allowed(skill_tool):
            registry.register(skill_tool)
        for checkpoint_tool in [
            CheckpointListTool(checkpoint_store),
            CheckpointRewindTool(checkpoint_store),
        ]:
            if _allowed(checkpoint_tool):
                registry.register(checkpoint_tool)

        child_task_manager = task_manager
        for t in [
            TaskCreateTool(child_task_manager),
            TaskClaimTool(child_task_manager),
            TaskUpdateTool(child_task_manager),
            TaskListTool(child_task_manager),
            TaskGetTool(child_task_manager),
        ]:
            if _allowed(t):
                registry.register(t)

        nested = SpawnAgentTool(
            provider=provider or self._provider,
            parent_bus=child_bus,
            parent_run_id=child_run_id,
            permission_manager=self._permission_manager,
            max_steps=self._max_steps,
            task_registry=self._task_registry,
            runs_dir=self._runs_dir,
            session_id=self._session_id,
            depth=self._depth + 1,
            workspace_boundary=boundary,
            task_manager=task_manager,
            route_registry=self._route_registry,
            hooks=self._hooks,
            max_depth=self._max_depth,
            interaction_manager=self._interaction_manager,
            authority_ceiling=authority_ceiling,
            route_binding=route_binding,
            require_route_readiness=self._require_route_readiness,
        )
        from code_rook.core.subagent.agent import AgentTool

        if _allowed("agent") or _allowed("spawn_agent"):
            registry.register(AgentTool(self._task_registry, nested))
        if _allowed("spawn_agent"):
            registry.register(
                nested,
                spec=nested.build_spec().model_copy(
                    update={
                        "model_visible": False,
                        "allowed_callers": frozenset(
                            {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                        ),
                    }
                ),
            )
        if _allowed("agent_result"):
            result_tool = AgentResultTool(self._task_registry)
            registry.register(
                result_tool,
                spec=result_tool.build_spec().model_copy(
                    update={
                        "model_visible": False,
                        "allowed_callers": frozenset(
                            {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                        ),
                    }
                ),
            )

        search_tool = ToolSearchTool(registry)
        if _allowed(search_tool):
            registry.register(search_tool)

        return registry

    # 优先通过运行中纠偏队列发送 followup，未注册时回退到直接上下文注入
    def followup(self, worker_id: str, message: str) -> WorkerRecord:
        if self._interaction_manager is not None and self._interaction_manager.steer(
            worker_id, message
        ):
            worker = self._task_registry.record(worker_id)
            if worker is None:
                raise ValueError(f"worker not found: {worker_id}")
            return self._task_registry.record_followup(worker_id, message)
        return self._task_registry.followup(worker_id, message)


class AgentResultParams(BaseModel):
    run_id: str


# 查询后台 subagent 的执行状态和最终结果
class AgentResultTool(BaseTool):
    name = "agent_result"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Returns 'still running' if the sub-agent has not yet completed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent(run_in_background=true)",
            },
        },
        "required": ["run_id"],
    }
    params_model = AgentResultParams

    # 初始化，持有共享的后台任务注册表
    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    # 查询指定 run_id 的后台任务状态，返回结果或错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        entry = self._task_registry.get(p.run_id)
        worker = self._task_registry.record(p.run_id)
        if entry is None and worker is None:
            return ToolResult(
                content=f"Unknown run_id: {p.run_id}. Only background subagents can be queried.",
                is_error=True,
                error_type="runtime_error",
            )
        if entry is not None:
            task, _context = entry
            if not task.done():
                return ToolResult(content="still running")
        worker = self._task_registry.record(p.run_id)
        if worker is None:
            return ToolResult(
                content=f"Worker record unavailable: {p.run_id}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(
            content=worker_result_payload(worker),
            is_error=worker.status
            in {
                WorkerStatus.FAILED,
                WorkerStatus.CANCELLED,
                WorkerStatus.BUDGET_LIMITED,
            },
            error_type=(
                "runtime_error"
                if worker.status
                in {
                    WorkerStatus.FAILED,
                    WorkerStatus.CANCELLED,
                    WorkerStatus.BUDGET_LIMITED,
                }
                else None
            ),
        )

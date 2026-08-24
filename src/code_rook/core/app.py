from __future__ import annotations

import argparse
import asyncio
import datetime
import fnmatch
import hmac
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

import code_rook
from code_rook.core.agents.loader import AgentProfileLoader
from code_rook.core.api import HttpApiServer, RuntimeApiService
from code_rook.core.api.auth import load_or_create_api_token
from code_rook.core.artifacts import ArtifactStore
from code_rook.core.artifacts.store import scan_referenced_artifact_shas
from code_rook.core.audit import AuditHealth, AuditIncident
from code_rook.core.authority import RuntimeMode, ToolAction, WorkspaceTrust
from code_rook.core.authority.sandbox import detect_sandbox_capability
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    ArtifactGcCommand,
    ArtifactGcResult,
    ArtifactListCommand,
    ArtifactListResult,
    BackgroundCancelCommand,
    BackgroundCancelResult,
    BackgroundGetCommand,
    BackgroundGetResult,
    BackgroundJobInfo,
    CoreShutdownCommand,
    CoreShutdownResult,
    EventReplayCommand,
    EventReplayResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    EventUnsubscribeCommand,
    EventUnsubscribeResult,
    GoalActionResult,
    GoalClearCommand,
    GoalCompleteCommand,
    GoalContinueDecisionCommand,
    GoalContinueDecisionResult,
    GoalCreateCommand,
    GoalCreateResult,
    GoalEditCommand,
    GoalGetCommand,
    GoalGetResult,
    GoalListCommand,
    GoalListResult,
    GoalPauseCommand,
    GoalResumeCommand,
    HookAuditInfo,
    HookConfigInfo,
    HookRerunCommand,
    HookRerunResult,
    HooksListCommand,
    HooksListResult,
    McpListCommand,
    McpListResult,
    McpServerInfo,
    MemoryAddCommand,
    MemoryDeleteCommand,
    MemoryDeleteResult,
    MemoryEditCommand,
    MemoryExpireCommand,
    MemoryInfo,
    MemoryListCommand,
    MemoryListResult,
    MemoryMutationResult,
    MemoryPinCommand,
    MemorySettingsGetCommand,
    MemorySettingsInfo,
    MemorySettingsResult,
    MemorySettingsSetCommand,
    PermissionRespondCommand,
    PermissionRespondResult,
    PlanRespondCommand,
    PlanRespondResult,
    PongResult,
    RunCancelCommand,
    RunCancelResult,
    RunSteerCommand,
    RunSteerResult,
    RuntimeCapabilitiesCommand,
    RuntimeCapabilitiesResult,
    SessionAuthorityResult,
    SessionCheckpointsCommand,
    SessionCheckpointsResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionContextCommand,
    SessionContextResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionDeleteCommand,
    SessionDeleteResult,
    SessionExportCommand,
    SessionExportResult,
    SessionForkCommand,
    SessionForkResult,
    SessionGetAuthorityCommand,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionInfo,
    SessionListCommand,
    SessionListResult,
    SessionRenameCommand,
    SessionRenameResult,
    SessionResumeCommand,
    SessionResumeResult,
    SessionRewindCommand,
    SessionRewindPreviewCommand,
    SessionRewindPreviewResult,
    SessionRewindResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
    SessionSetAuthorityCommand,
    SessionTasksCommand,
    SessionTasksResult,
    ThreadArchiveCommand,
    ThreadArchiveResult,
    ThreadCreateCommand,
    ThreadCreateResult,
    ThreadGetCommand,
    ThreadGetResult,
    ThreadListCommand,
    ThreadListResult,
    ThreadUpdateCommand,
    ThreadUpdateResult,
    TurnGetCommand,
    TurnGetResult,
    TurnInspectCommand,
    TurnInspectResult,
    TurnInterruptCommand,
    TurnInterruptResult,
    TurnItemsCommand,
    TurnItemsResult,
    TurnListCommand,
    TurnListResult,
    TurnStartCommand,
    TurnStartResult,
    TurnSteerCommand,
    TurnSteerResult,
    UserQuestionRespondCommand,
    UserQuestionRespondResult,
    WorkerApplyCommand,
    WorkerApplyResult,
    WorkerCancelCommand,
    WorkerCancelResult,
    WorkerEventsCommand,
    WorkerEventsResult,
    WorkerFollowupCommand,
    WorkerFollowupResult,
    WorkerListCommand,
    WorkerListResult,
    WorkerRetryCommand,
    WorkerRetryResult,
    WorkerReviewCommand,
    WorkerReviewResult,
    WorkerStartCommand,
    WorkerStartResult,
    WorkerStatusCommand,
    WorkerStatusResult,
    WorkflowGetCommand,
    WorkflowGetResult,
    WorkflowListCommand,
    WorkflowListResult,
    WorkflowStartCommand,
    WorkflowStartResult,
    WorkspaceCommitCommand,
    WorkspaceCommitResult,
    WorkspaceDiffCommand,
    WorkspaceDiffResult,
    WorkspaceStageCommand,
    WorkspaceStageResult,
)
from code_rook.core.bus.envelope import INVALID_PARAMS, EventPushEnvelope, HandlerError
from code_rook.core.bus.events import (
    AuditDegradedEvent,
    VerificationCompletedEvent,
)
from code_rook.core.capabilities import (
    CapabilityContribution,
    CapabilityKernel,
    CapabilityKind,
    CapabilityScope,
    CapabilityStability,
)
from code_rook.core.change_center import ChangeCenterError, ChangeCenterService
from code_rook.core.compatibility import build_runtime_capabilities
from code_rook.core.config import CodeRookConfig, get_config
from code_rook.core.daemon_lock import DaemonLock, DaemonLockError
from code_rook.core.events.bus import EventBus
from code_rook.core.features import labs_enabled
from code_rook.core.fleet import (
    FleetProfile,
    LocalFleet,
    LocalFleetScheduler,
    LocalProcessHost,
    SQLiteWorkerStore,
)
from code_rook.core.goal import GoalService, GoalStore, GoalStoreError
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import HeadlessQuestionPolicy, InteractionManager
from code_rook.core.llm.credentials import CredentialStoreError, llm_is_configured
from code_rook.core.llm.migration_receipt import ProviderCatalogMigrationReceiptError
from code_rook.core.llm.route_registry import RouteRegistry, RouteResolutionError
from code_rook.core.llm.route_store import RouteStoreError
from code_rook.core.logging_setup import setup_logging
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.memory import MemoryRecord, MemoryStore
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.permissions.storage import load_policy_file
from code_rook.core.persistent_shell import PersistentShellPool
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.runner import AgentRunner
from code_rook.core.runs import events_file, new_run_id
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore, RuntimeStoreError
from code_rook.core.sandbox.planner import SandboxTier, plan_sandbox
from code_rook.core.session import Session, SessionManager, SessionStore
from code_rook.core.state_migration import migrate_legacy_state
from code_rook.core.state_paths import (
    StatePathSecurityError,
    prepare_user_state_layout,
)
from code_rook.core.subagent.backends import AcpWorkerBackend, WorkerBackendRegistry
from code_rook.core.subagent.controller import WorkerController, WorkerControllerError
from code_rook.core.subagent.models import WorkerRecord
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.store import WorkerStoreError
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.writer import TraceWriter
from code_rook.core.transport.auth import load_or_create_ipc_token, require_loopback_host
from code_rook.core.transport.ipc_broadcaster import IpcEventBroadcaster
from code_rook.core.transport.socket_server import SocketServer, get_connection_writer
from code_rook.core.upgrade import (
    UpgradeStateLockError,
    ensure_v1_upgrade_backup,
    v1_state_mutation,
)
from code_rook.core.workflow import (
    WorkflowLedger,
    WorkflowLedgerError,
    WorkflowParseError,
    parse_workflow_text,
)
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.core.worktree import (
    WorktreeApplyStateError,
    WorktreeError,
    WorktreeManager,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


# 将 WorkerRecord 转成不含 prompt、凭据和完整 transcript 的控制面状态
def _worker_status_payload(item: WorkerRecord) -> dict[str, Any]:
    return {
        "worker_id": item.id,
        "session_id": item.session_id,
        "parent_turn_id": item.parent_turn_id,
        "root_goal_id": item.root_goal_id,
        "description": item.description,
        "role": item.role,
        "profile": item.profile,
        "profile_digest": item.profile_digest,
        "route": item.route,
        "route_digest": item.route_digest,
        "model": item.model,
        "reasoning": item.reasoning,
        "status": item.status.value,
        "status_reason": item.status_reason,
        "depth": item.depth,
        "attempt": item.attempt,
        "max_attempts": item.max_attempts,
        "worktree": item.worktree,
        "branch": item.branch,
        "base_commit": item.base_commit,
        "read_only": item.write_claim.read_only,
        "write_claim": item.write_claim.model_dump(mode="json"),
        "handoff_status": item.handoff_status,
        "changed_files": item.changed_files[:100],
        "diff_stat": item.diff_stat[:4_000],
        "diff_preview": item.diff_preview[:20_000],
        "diff_truncated": item.diff_truncated,
        "verification_status": item.verification_status,
        "approved": item.approved,
        "review_digest": item.review_digest,
        "token_budget": item.token_budget,
        "token_usage": item.token_usage,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "cache_read_input_tokens": item.cache_read_input_tokens,
        "cache_creation_input_tokens": item.cache_creation_input_tokens,
        "estimated_cost_usd": item.estimated_cost_usd,
        "cost_status": item.cost_status,
        "heartbeat_at": item.heartbeat_at,
        "event_cursor": item.event_cursor,
        "summary": item.summary[:1_000],
        "blockers": item.blockers[:10],
    }


class CoreApp:
    # 初始化 daemon 依赖，并保存仅由显式 CLI 传入的环境文件路径
    def __init__(self, *, env_file: Path | None = None) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._audit_health = AuditHealth(self._publish_audit_incident)
        self._daemon_lock: DaemonLock | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: CodeRookConfig | None = None
        self._env_file = env_file
        self._labs_enabled = labs_enabled()
        self._capability_kernel = CapabilityKernel()
        self._capability_scope = CapabilityScope(
            workspace=str(Path.cwd().resolve())
        )
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._goal_service: GoalService | None = None
        self._runtime: RuntimeService | None = None
        self._route_registry: RouteRegistry | None = None
        self._permission_manager: PermissionManager | None = None
        self._hooks: HookManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._process_supervisor = ProcessSupervisor()
        self._artifact_store = ArtifactStore(Path.cwd() / ".coderook" / "artifacts")
        self._persistent_shell_pool = PersistentShellPool(
            process_supervisor=self._process_supervisor,
            artifact_store=self._artifact_store,
        )
        self._background_registry = BackgroundJobRegistry(
            self._bus,
            self._process_supervisor,
            artifact_store=self._artifact_store,
        )
        self._interaction_manager = InteractionManager(self._bus)
        # daemon 级后台 subagent 任务注册表，跨 turn 持有
        self._subagent_registry: BackgroundTaskRegistry | None = None
        self._worker_controller: WorkerController | None = None
        self._worker_backends = WorkerBackendRegistry(
            self._capability_kernel,
            scope=self._capability_scope,
        )
        self._fleet_registry: BackgroundTaskRegistry | None = None
        self._fleet: LocalFleet | None = None
        self._http_api: HttpApiServer | None = None
        self._runtime_api: RuntimeApiService | None = None

    # 将审计故障转换为不含异常正文的全局可见事件
    async def _publish_audit_incident(self, incident: AuditIncident) -> None:
        await self._bus.publish(
            AuditDegradedEvent(
                source=incident.source,
                diagnostic_id=incident.diagnostic_id,
                error_type=incident.error_type,
                ts=incident.ts,
            )
        )

    # 注册并重新解析 workspace capability，确保实际消费者经过同一 Kernel seam
    def _register_workspace_capability(
        self,
        kind: CapabilityKind,
        contribution_id: str,
        provider: object,
        *,
        stability: CapabilityStability,
    ) -> object:
        self._capability_kernel.register(
            CapabilityContribution(
                id=contribution_id,
                kind=kind.value,
                provider=provider,
                stability=stability,
                scope=self._capability_scope,
            )
        )
        resolved = self._capability_kernel.resolve(
            kind,
            contribution_id,
            self._capability_scope,
        )
        if resolved is None:
            raise RuntimeError(
                f"capability registration was not resolvable: {kind.value}/{contribution_id}"
            )
        return resolved

    # 收集 session 与工作区元数据文件，供 artifact GC 建立引用保留集合
    def _artifact_reference_paths(self) -> list[Path]:
        roots = [
            Path("~/.coderook/sessions").expanduser(),
            Path.cwd() / ".coderook",
        ]
        artifact_root = (Path.cwd() / ".coderook" / "artifacts").resolve()
        paths: list[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    if path.resolve().is_relative_to(artifact_root):
                        continue
                except OSError:
                    continue
                paths.append(path)
        return paths

    # 返回 artifact 清单、总大小和按当前保留期可回收大小
    async def _artifact_list_handler(self, params: dict[str, Any]) -> ArtifactListResult:
        cmd = ArtifactListCommand.model_validate(params)
        keep = await asyncio.to_thread(
            scan_referenced_artifact_shas,
            self._artifact_reference_paths(),
        )
        inventory = await asyncio.to_thread(
            self._artifact_store.inventory,
            days=cmd.days,
            keep=keep,
        )
        return ArtifactListResult(
            artifacts=inventory,
            total_bytes=sum(item.size for item in inventory),
            reclaimable_bytes=sum(item.size for item in inventory if item.gc_candidate),
        )

    # 默认预览 GC；确认后重新扫描引用、删除候选并追加脱敏 receipt
    async def _artifact_gc_handler(self, params: dict[str, Any]) -> ArtifactGcResult:
        cmd = ArtifactGcCommand.model_validate(params)
        first = await self._artifact_list_handler({"days": cmd.days})
        candidates = [item.sha256 for item in first.artifacts if item.gc_candidate]
        if not cmd.confirmed:
            return ArtifactGcResult(
                dry_run=True,
                candidates=candidates,
                reclaimable_bytes=first.reclaimable_bytes,
            )

        keep = await asyncio.to_thread(
            scan_referenced_artifact_shas,
            self._artifact_reference_paths(),
        )
        second_inventory = await asyncio.to_thread(
            self._artifact_store.inventory,
            days=cmd.days,
            keep=keep,
        )
        candidate_sizes = {
            item.sha256: item.size for item in second_inventory if item.gc_candidate
        }
        removed_paths = await asyncio.to_thread(
            self._artifact_store.gc,
            days=cmd.days,
            keep=keep,
            dry_run=False,
        )
        removed = [path.name for path in removed_paths]
        reclaimed = sum(candidate_sizes.get(sha256, 0) for sha256 in removed)
        receipt_path = Path("~/.coderook/artifact-gc-receipts.jsonl").expanduser()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 1,
            "ts": _now(),
            "workspace": str(Path.cwd()),
            "days": cmd.days,
            "removed": removed,
            "reclaimed_bytes": reclaimed,
        }
        with receipt_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        return ArtifactGcResult(
            dry_run=False,
            candidates=[item.sha256 for item in second_inventory if item.gc_candidate],
            removed=removed,
            reclaimable_bytes=reclaimed,
            receipt_path=str(receipt_path),
        )

    # 从当前 route、agent profile 和 authority 真值构建不可变 FleetProfile
    def _build_fleet_profiles(self) -> list[FleetProfile]:
        assert self._route_registry is not None
        assert self._permission_manager is not None
        default_route = self._route_registry.route()
        default_authority = self._permission_manager.get_authority_snapshot("")
        profiles = {
            "default": FleetProfile(
                name="default",
                route=default_route.id,
                model=default_route.model,
                authority_ceiling=default_authority,
            )
        }
        for agent in AgentProfileLoader(Path.cwd()).list_all():
            route = (
                self._route_registry.route(agent.route)
                if agent.route
                else default_route
            )
            authority = default_authority
            if agent.restrict == "read_only":
                authority = authority.model_copy(
                    update={
                        "mode": RuntimeMode.PLAN,
                        "allowed_actions": frozenset({ToolAction.READ}),
                    }
                )
            profiles[agent.name] = FleetProfile(
                name=agent.name,
                route=route.id,
                model=agent.model or route.model,
                reasoning=agent.reasoning,
                authority_ceiling=authority,
            )
        return list(profiles.values())

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=code_rook.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
            workspace=str(Path.cwd().resolve()),
            active_runs=(
                self._sessions.active_run_count() if self._sessions is not None else 0
            ),
        )

    # 处理 core.shutdown 请求：触发有序关闭流程并立即返回确认
    async def _shutdown_handler(self, params: dict[str, Any]) -> CoreShutdownResult:
        CoreShutdownCommand.model_validate(params)
        logger.info("shutdown requested via IPC")
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        return CoreShutdownResult()

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        assert self._permission_manager is not None
        cmd = AgentRunCommand.model_validate(params)
        session = (
            await self._sessions.resume(cmd.resume_session_id)
            if cmd.resume_session_id is not None
            else await self._sessions.create(mode="one_shot", title=cmd.goal[:40])
        )
        self._permission_manager.set_session_mode(
            session.id,
            cmd.permission_mode,
            allow_tools=cmd.allow_tools,
        )
        self._interaction_manager.set_question_policy(
            session.id,
            HeadlessQuestionPolicy(
                mode=cmd.question_mode,
                timeout_s=cmd.question_timeout_s,
                answers=tuple(cmd.preset_answers),
            ),
        )
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)

        def _cleanup(completed: asyncio.Task[Any]) -> None:
            self._running_runs.discard(completed)
            if self._permission_manager is not None:
                self._permission_manager.clear_session_mode(session.id)
            self._interaction_manager.clear_question_policy(session.id)

        run_task.add_done_callback(_cleanup)
        return AgentRunResult(run_id=run_id, session_id=session.id)

    # 将 daemon 验证通过事件写入当前 run 关联的 Goal
    async def _goal_usage_event_handler(self, event: BaseModel) -> None:
        if self._goal_service is None or not isinstance(
            event,
            VerificationCompletedEvent,
        ):
            return
        goal = next(
            (
                item
                for item in self._goal_service.list_all()
                if item.current_run_id == event.run_id
            ),
            None,
        )
        if goal is None:
            return
        observed_criteria = {event.action.strip()}
        covered_criteria = [
            criterion
            for criterion in goal.completion_criteria
            if criterion.strip() in observed_criteria
        ]
        self._goal_service.record_verification(
            goal.id,
            run_id=event.run_id,
            step=event.step,
            tool=event.tool,
            action=event.action,
            summary=f"{event.passed}/{event.gate_count} verification gates passed",
            covered_criteria=covered_criteria,
        )

    # 创建持久 Goal，并按请求在所属 session 中立即开始首轮执行
    async def _goal_create_handler(self, params: dict[str, Any]) -> GoalCreateResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        assert self._permission_manager is not None
        cmd = GoalCreateCommand.model_validate(params)
        self._sessions.get_session(cmd.session_id)
        if self._sessions.is_busy(cmd.session_id):
            raise HandlerError(INVALID_PARAMS, "cannot create goal during an active turn")
        try:
            goal = self._goal_service.create(
                cmd.objective,
                session_id=cmd.session_id,
                token_budget=cmd.token_budget,
                auto_continue=cmd.auto_continue,
                max_auto_turns=cmd.max_auto_turns,
                max_wall_seconds=cmd.max_wall_seconds,
                permission_ceiling=self._permission_manager.get_authority_snapshot(
                    cmd.session_id
                ),
                constraints=cmd.constraints,
                completion_criteria=cmd.completion_criteria,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

        run_id: str | None = None
        if cmd.start:
            try:
                run_id = await self._sessions.send_message(cmd.session_id, goal.objective)
            except HandlerError:
                self._goal_service.set_status(goal.id, "blocked", actor="system")
                raise
            goal = self._goal_service.get(goal.id)
        return GoalCreateResult(goal=goal, run_id=run_id)

    # 查询并持久化有限 Goal Loop 的下一轮安全决策，不直接启动模型
    async def _goal_continue_decision_handler(
        self,
        params: dict[str, Any],
    ) -> GoalContinueDecisionResult:
        assert self._goal_service is not None
        assert self._permission_manager is not None
        cmd = GoalContinueDecisionCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            decision = self._goal_service.decide_continue(
                selected.id,
                current_authority=self._permission_manager.get_authority_snapshot(
                    selected.session_id
                ),
            )
            return GoalContinueDecisionResult(
                goal=self._goal_service.get(selected.id),
                decision=decision,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 查询 Goal ID 或 session 当前 Goal，不存在的 session Goal 返回空结果
    async def _goal_get_handler(self, params: dict[str, Any]) -> GoalGetResult:
        assert self._goal_service is not None
        cmd = GoalGetCommand.model_validate(params)
        try:
            if cmd.goal_id.strip():
                return GoalGetResult(goal=self._goal_service.get(cmd.goal_id.strip()))
            return GoalGetResult(goal=self._goal_service.current(cmd.session_id.strip()))
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 按 session、状态和数量稳定列出持久 Goal
    async def _goal_list_handler(self, params: dict[str, Any]) -> GoalListResult:
        assert self._goal_service is not None
        cmd = GoalListCommand.model_validate(params)
        goals = self._goal_service.list_all()
        if cmd.session_id.strip():
            goals = [goal for goal in goals if goal.session_id == cmd.session_id.strip()]
        if cmd.status is not None:
            goals = [goal for goal in goals if goal.status == cmd.status]
        return GoalListResult(goals=goals[-cmd.limit :])

    # 修改当前 Goal，并把新目标实时纠偏到正在执行的 run
    async def _goal_edit_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalEditCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            goal = self._goal_service.edit(
                selected.id,
                cmd.objective,
                completion_criteria=cmd.completion_criteria,
            )
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.steer_run(
                    run_id,
                    f"The active goal was edited. New objective: {goal.objective}",
                )
            return GoalActionResult(goal=goal, run_id=run_id)
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 暂停 Goal，并取消当前 run 以确保暂停立即生效
    async def _goal_pause_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalPauseCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            goal = self._goal_service.pause(selected.id)
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.cancel_run(run_id)
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 恢复暂停或阻塞 Goal，并在所属 session 中开始新的继续轮次
    async def _goal_resume_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalResumeCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            if self._sessions.is_busy(selected.session_id):
                raise ValueError("cannot resume goal during an active turn")
            goal = self._goal_service.resume(selected.id)
            try:
                run_id = await self._sessions.send_message(
                    goal.session_id,
                    "Continue working toward the active goal and verify every "
                    "completion criterion.",
                )
            except HandlerError:
                latest = self._goal_service.get(goal.id)
                if latest.status == "active":
                    self._goal_service.set_status(goal.id, "blocked", actor="system")
                raise
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 清除 Goal，并取消仍在运行的关联 turn，同时保留审计文件
    async def _goal_clear_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalClearCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            goal = self._goal_service.clear(selected.id)
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.cancel_run(run_id)
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 由用户显式确认完成 Goal，并使用最近一次 run 或用户确认作为审计证据
    async def _goal_complete_handler(self, params: dict[str, Any]) -> GoalActionResult:
        assert self._sessions is not None
        assert self._goal_service is not None
        cmd = GoalCompleteCommand.model_validate(params)
        try:
            selected = self._goal_service.resolve(
                goal_id=cmd.goal_id,
                session_id=cmd.session_id,
            )
            reference = (
                f"run://{selected.linked_run_ids[-1]}"
                if selected.linked_run_ids
                else "user://confirmation"
            )
            goal = self._goal_service.complete(
                selected.id,
                evidence=[("user-confirmation", reference)],
                summary=cmd.summary or "User confirmed the goal is complete.",
                actor="user",
            )
            run_id = self._sessions.active_run_id(goal.session_id)
            if run_id is not None:
                await self._sessions.cancel_run(run_id)
            return GoalActionResult(
                goal=self._goal_service.get(goal.id),
                run_id=run_id,
            )
        except (GoalStoreError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc

    # 取消指定 active run，并等待 Session 状态稳定落盘
    async def _run_cancel_handler(self, params: dict[str, Any]) -> RunCancelResult:
        assert self._sessions is not None
        cmd = RunCancelCommand.model_validate(params)
        session_id = await self._sessions.cancel_run(cmd.run_id)
        return RunCancelResult(run_id=cmd.run_id, session_id=session_id)

    # 将新用户指令排入活动 run 的纠偏队列
    async def _run_steer_handler(self, params: dict[str, Any]) -> RunSteerResult:
        assert self._sessions is not None
        cmd = RunSteerCommand.model_validate(params)
        await self._sessions.steer_run(cmd.run_id, cmd.content)
        return RunSteerResult(run_id=cmd.run_id)

    # 按 thread 事件游标分页返回已持久化 runtime 事件
    async def _event_replay_handler(self, params: dict[str, Any]) -> EventReplayResult:
        assert self._runtime is not None
        cmd = EventReplayCommand.model_validate(params)
        events = await self._runtime.list_events(
            cmd.thread_id,
            after_seq=cmd.after_seq,
            limit=cmd.limit,
        )
        latest_seq = await self._runtime.latest_event_seq(cmd.thread_id)
        last_seq = events[-1].seq if events else cmd.after_seq
        return EventReplayResult(
            events=events,
            latest_seq=latest_seq,
            has_more=last_seq < latest_seq,
        )

    # 创建兼容 session 并返回同一 durable thread
    async def _thread_create_handler(self, params: dict[str, Any]) -> ThreadCreateResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = ThreadCreateCommand.model_validate(params)
        if cmd.preset_id == "tool-program" and not self._labs_enabled:
            raise HandlerError(INVALID_PARAMS, "tool-program preset requires CODEROOK_LABS=1")
        session = await self._sessions.create(
            mode=cmd.mode,
            title=cmd.title,
            preset_id=cmd.preset_id,
        )
        return ThreadCreateResult(thread=await self._runtime.get_thread(session.id))

    # 列出 durable threads 并按归档状态和数量裁剪
    async def _thread_list_handler(self, params: dict[str, Any]) -> ThreadListResult:
        assert self._runtime is not None
        cmd = ThreadListCommand.model_validate(params)
        threads = await self._runtime.list_threads()
        if not cmd.include_archived:
            threads = [thread for thread in threads if thread.status.value != "archived"]
        return ThreadListResult(threads=threads[: cmd.limit])

    # 查询单个 durable thread
    async def _thread_get_handler(self, params: dict[str, Any]) -> ThreadGetResult:
        assert self._runtime is not None
        cmd = ThreadGetCommand.model_validate(params)
        return ThreadGetResult(thread=await self._runtime.get_thread(cmd.thread_id))

    # 通过 session facade 更新标题并返回 durable thread 投影
    async def _thread_update_handler(self, params: dict[str, Any]) -> ThreadUpdateResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = ThreadUpdateCommand.model_validate(params)
        await self._sessions.rename(cmd.thread_id, cmd.title)
        return ThreadUpdateResult(thread=await self._runtime.get_thread(cmd.thread_id))

    # 通过 session facade 归档 thread 并返回持久终态
    async def _thread_archive_handler(self, params: dict[str, Any]) -> ThreadArchiveResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = ThreadArchiveCommand.model_validate(params)
        await self._sessions.close(cmd.thread_id)
        return ThreadArchiveResult(thread=await self._runtime.get_thread(cmd.thread_id))

    # 异步启动 turn 并立即返回稳定 turn id
    async def _turn_start_handler(self, params: dict[str, Any]) -> TurnStartResult:
        assert self._sessions is not None
        cmd = TurnStartCommand.model_validate(params)
        turn_id = new_run_id()
        task = asyncio.create_task(
            self._sessions.send_message(
                cmd.thread_id,
                cmd.content,
                run_id=turn_id,
                runtime_mode=cmd.runtime_mode,
            )
        )
        self._running_runs.add(task)
        task.add_done_callback(self._running_runs.discard)
        return TurnStartResult(turn_id=turn_id)

    # 查询单个 durable turn
    async def _turn_get_handler(self, params: dict[str, Any]) -> TurnGetResult:
        assert self._runtime is not None
        cmd = TurnGetCommand.model_validate(params)
        return TurnGetResult(turn=await self._runtime.get_turn(cmd.turn_id))

    # 列出 thread 的全部 durable turns
    async def _turn_list_handler(self, params: dict[str, Any]) -> TurnListResult:
        assert self._runtime is not None
        cmd = TurnListCommand.model_validate(params)
        return TurnListResult(turns=await self._runtime.list_turns(cmd.thread_id))

    # 中断活动 turn 并返回持久终态
    async def _turn_interrupt_handler(
        self,
        params: dict[str, Any],
    ) -> TurnInterruptResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = TurnInterruptCommand.model_validate(params)
        await self._sessions.cancel_run(cmd.turn_id)
        return TurnInterruptResult(turn=await self._runtime.get_turn(cmd.turn_id))

    # 向活动 turn 排入纠偏指令并返回当前持久状态
    async def _turn_steer_handler(self, params: dict[str, Any]) -> TurnSteerResult:
        assert self._sessions is not None
        assert self._runtime is not None
        cmd = TurnSteerCommand.model_validate(params)
        await self._sessions.steer_run(cmd.turn_id, cmd.content)
        return TurnSteerResult(turn=await self._runtime.get_turn(cmd.turn_id))

    # 返回 turn 的全部 durable items
    async def _turn_items_handler(self, params: dict[str, Any]) -> TurnItemsResult:
        assert self._runtime is not None
        cmd = TurnItemsCommand.model_validate(params)
        await self._runtime.get_turn(cmd.turn_id)
        return TurnItemsResult(items=await self._runtime.list_items(cmd.turn_id))

    # 返回客户端可协商的统一 runtime 能力
    async def _runtime_capabilities_handler(
        self,
        params: dict[str, Any],
    ) -> RuntimeCapabilitiesResult:
        RuntimeCapabilitiesCommand.model_validate(params)
        sandbox = (
            self._permission_manager.get_authority_snapshot(
                "__runtime_capabilities__"
            ).sandbox
            if self._permission_manager is not None
            else None
        )
        snapshot = build_runtime_capabilities(
            code_rook.__version__,
            sandbox=sandbox,
            labs_enabled=self._labs_enabled,
        )
        return RuntimeCapabilitiesResult.model_validate(snapshot.model_dump())

    # 拒绝在当前 daemon 未显式开启 Labs 时访问实验性控制面
    def _require_labs(self, feature: str) -> None:
        if not self._labs_enabled:
            raise HandlerError(
                INVALID_PARAMS,
                f"{feature} is unavailable because Labs is disabled",
            )

    # 按 Labs 开关构造 hooks manager，关闭时绝不读取用户或项目 hooks 配置
    def _build_hook_manager(self, workspace: Path) -> HookManager:
        # 复用当前会话的工作区信任快照判断项目 hook 是否可执行
        def trust_provider(session_id: str) -> bool:
            return (
                self._permission_manager is not None
                and self._permission_manager.get_authority_snapshot(
                    session_id
                ).workspace_trust
                == WorkspaceTrust.TRUSTED
            )
        if not self._labs_enabled:
            return HookManager(
                [],
                workspace=workspace,
                bus=self._bus,
                project_trust_provider=trust_provider,
                process_supervisor=self._process_supervisor,
            )
        return HookManager.from_workspace(
            workspace,
            bus=self._bus,
            project_trust_provider=trust_provider,
            process_supervisor=self._process_supervisor,
        )

    # 返回严格属于指定会话的 Worker，Fleet 仅在 Labs 开启且调用方明确允许时可见
    def _worker_for_session(
        self,
        session_id: str,
        worker_id: str,
        *,
        allow_fleet: bool = False,
    ) -> WorkerRecord:
        assert self._subagent_registry is not None
        worker = self._subagent_registry.record(worker_id)
        if (
            worker is None
            and allow_fleet
            and self._labs_enabled
            and self._fleet_registry is not None
        ):
            worker = self._fleet_registry.record(worker_id)
        if worker is None:
            raise HandlerError(INVALID_PARAMS, f"worker not found: {worker_id}")
        if not worker.session_id or worker.session_id != session_id:
            raise HandlerError(
                INVALID_PARAMS,
                f"worker not found for session: {worker_id}",
            )
        return worker

    # 仅在 Labs 显式开启时恢复持久 workflow，并返回本次恢复数量
    def _resume_labs_workflows(self) -> int:
        if not self._labs_enabled or self._fleet is None:
            return 0
        resumed = self._fleet.resume_all()
        if resumed:
            logger.info("resumed %d durable workflows", len(resumed))
        return len(resumed)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        if cmd.preset_id == "tool-program" and not self._labs_enabled:
            raise HandlerError(INVALID_PARAMS, "tool-program preset requires CODEROOK_LABS=1")
        session = await self._sessions.create(
            mode=cmd.mode,
            title=cmd.title,
            preset_id=cmd.preset_id,
        )
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(
            cmd.session_id,
            cmd.content,
            runtime_mode=cmd.runtime_mode,
            attachments=cmd.attachments,
        )
        return SessionSendMessageResult(run_id=run_id)

    # 返回指定会话从下一轮开始使用的权限快照
    async def _session_get_authority_handler(
        self,
        params: dict[str, Any],
    ) -> SessionAuthorityResult:
        assert self._sessions is not None
        assert self._permission_manager is not None
        cmd = SessionGetAuthorityCommand.model_validate(params)
        self._sessions.get_session(cmd.session_id)
        return SessionAuthorityResult(
            snapshot=self._permission_manager.get_authority_snapshot(cmd.session_id)
        )

    # 独立更新后续轮次的 mode、profile 或 trust，并保留未指定维度
    async def _session_set_authority_handler(
        self,
        params: dict[str, Any],
    ) -> SessionAuthorityResult:
        assert self._sessions is not None
        assert self._permission_manager is not None
        cmd = SessionSetAuthorityCommand.model_validate(params)
        self._sessions.get_session(cmd.session_id)
        if self._sessions.is_busy(cmd.session_id):
            raise HandlerError(INVALID_PARAMS, "authority cannot change during an active turn")
        current = self._permission_manager.get_authority_snapshot(cmd.session_id)
        changes: dict[str, Any] = {}
        if cmd.mode is not None:
            changes["mode"] = cmd.mode
        if cmd.profile is not None:
            changes["profile"] = cmd.profile
        if cmd.workspace_trust is not None:
            changes["workspace_trust"] = cmd.workspace_trust
        updated = current.model_copy(update=changes)
        self._permission_manager.set_authority_snapshot(cmd.session_id, updated)
        return SessionAuthorityResult(snapshot=updated)

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    @staticmethod
    def _session_info(session: Session) -> SessionInfo:
        return SessionInfo(
            session_id=session.id,
            mode=session.mode,
            status=session.status,
            title=session.title,
            created_at=session.created_at,
            updated_at=session.updated_at,
            run_count=len(session.run_ids),
            last_run_id=session.run_ids[-1] if session.run_ids else None,
            parent_session_id=session.parent_session_id,
            workspace=session.workspace,
            preset_id=session.preset_id,
            preset_digest=session.preset_digest,
        )

    # 列出 daemon 已恢复的持久化 sessions
    async def _session_list_handler(self, params: dict[str, Any]) -> SessionListResult:
        assert self._sessions is not None
        cmd = SessionListCommand.model_validate(params)
        sessions = await self._sessions.list_sessions(
            include_closed=cmd.include_closed,
            limit=cmd.limit,
        )
        return SessionListResult(sessions=[self._session_info(session) for session in sessions])

    # 重新打开历史 chat session，后续消息会沿用其 thread
    async def _session_resume_handler(self, params: dict[str, Any]) -> SessionResumeResult:
        assert self._sessions is not None
        cmd = SessionResumeCommand.model_validate(params)
        session = await self._sessions.resume(cmd.session_id)
        return SessionResumeResult(session=self._session_info(session))

    async def _session_rename_handler(self, params: dict[str, Any]) -> SessionRenameResult:
        assert self._sessions is not None
        cmd = SessionRenameCommand.model_validate(params)
        session = await self._sessions.rename(cmd.session_id, cmd.title)
        return SessionRenameResult(session=self._session_info(session))

    async def _session_fork_handler(self, params: dict[str, Any]) -> SessionForkResult:
        assert self._sessions is not None
        cmd = SessionForkCommand.model_validate(params)
        if cmd.preset_id == "tool-program" and not self._labs_enabled:
            raise HandlerError(INVALID_PARAMS, "tool-program preset requires CODEROOK_LABS=1")
        session = await self._sessions.fork(
            cmd.session_id,
            cmd.title,
            preset_id=cmd.preset_id,
        )
        return SessionForkResult(session=self._session_info(session))

    async def _session_export_handler(self, params: dict[str, Any]) -> SessionExportResult:
        assert self._sessions is not None
        cmd = SessionExportCommand.model_validate(params)
        filename, media_type, content = await self._sessions.export(
            cmd.session_id,
            cmd.format,
        )
        return SessionExportResult(
            filename=filename,
            media_type=media_type,
            content=content,
        )

    async def _session_delete_handler(self, params: dict[str, Any]) -> SessionDeleteResult:
        assert self._sessions is not None
        cmd = SessionDeleteCommand.model_validate(params)
        await self._sessions.delete(cmd.session_id)
        return SessionDeleteResult(session_id=cmd.session_id)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.respond(
            cmd.tool_use_id,
            cmd.decision,
            selected_hunks=cmd.selected_hunks,
            patch_plan_id=cmd.patch_plan_id,
        )
        return PermissionRespondResult()

    # 接收用户对结构化问题的回答并恢复挂起的工具调用
    async def _user_question_respond_handler(
        self,
        params: dict[str, Any],
    ) -> UserQuestionRespondResult:
        cmd = UserQuestionRespondCommand.model_validate(params)
        if not self._interaction_manager.answer(cmd.question_id, cmd.answer):
            raise HandlerError(INVALID_PARAMS, "question is not pending")
        return UserQuestionRespondResult()

    # 只让 SessionManager 解决当前匹配 run 的计划审批并返回 durable 决定
    async def _plan_respond_handler(self, params: dict[str, Any]) -> PlanRespondResult:
        assert self._sessions is not None
        cmd = PlanRespondCommand.model_validate(params)
        resolved = await self._sessions.respond_plan(
            cmd.session_id,
            cmd.run_id,
            cmd.decision,
            cmd.revision,
        )
        return PlanRespondResult(
            session_id=resolved.session_id,
            run_id=resolved.run_id,
            decision=resolved.decision,
        )

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 返回当前会话最近一次 run 的任务列表
    async def _session_tasks_handler(self, params: dict[str, Any]) -> SessionTasksResult:
        assert self._sessions is not None
        cmd = SessionTasksCommand.model_validate(params)
        run_id, tasks = self._sessions.list_tasks(cmd.session_id)
        return SessionTasksResult(run_id=run_id, tasks=tasks)

    # 返回持久 Worker 列表，省略 prompt 和完整子运行 transcript
    async def _worker_list_handler(self, params: dict[str, Any]) -> WorkerListResult:
        assert self._subagent_registry is not None
        cmd = WorkerListCommand.model_validate(params)
        if cmd.worker_id:
            workers = [
                self._worker_for_session(
                    cmd.session_id,
                    cmd.worker_id,
                    allow_fleet=True,
                )
            ]
        else:
            workers = self._subagent_registry.list_records()
        if not cmd.worker_id and self._labs_enabled and self._fleet_registry is not None:
            workers.extend(self._fleet_registry.list_records())
        workers = [item for item in workers if item.session_id == cmd.session_id]
        if cmd.root_goal_id:
            workers = [
                item for item in workers if item.root_goal_id == cmd.root_goal_id
            ]
        payload = [_worker_status_payload(item) for item in workers[-cmd.limit :]]
        return WorkerListResult(workers=payload)

    # 通过 daemon-owned launcher 启动新的持久 Worker
    async def _worker_start_handler(self, params: dict[str, Any]) -> WorkerStartResult:
        assert self._worker_controller is not None
        cmd = WorkerStartCommand.model_validate(params)
        try:
            worker = await self._worker_controller.start(cmd)
        except (WorkerControllerError, RuntimeError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkerStartResult(
            worker_id=worker.id,
            session_id=worker.session_id,
            status=worker.status.value,
            route_id=worker.route,
            model=worker.model,
            attempt=worker.attempt,
            worktree=worker.worktree,
            read_only=worker.write_claim.read_only,
            backend=worker.backend,
            backend_capabilities=dict(worker.backend_capabilities),
            sandbox_enforcement=worker.sandbox_enforcement,
        )

    # 返回严格绑定当前 session 的单个 Worker 状态
    async def _worker_status_handler(self, params: dict[str, Any]) -> WorkerStatusResult:
        assert self._worker_controller is not None
        cmd = WorkerStatusCommand.model_validate(params)
        try:
            worker = self._worker_controller.status(cmd.session_id, cmd.worker_id)
        except (WorkerControllerError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkerStatusResult(worker=_worker_status_payload(worker))

    # 使用原 WorkerRecord 的不可变执行边界真正启动重试 attempt
    async def _worker_retry_handler(self, params: dict[str, Any]) -> WorkerRetryResult:
        assert self._worker_controller is not None
        cmd = WorkerRetryCommand.model_validate(params)
        try:
            worker = await self._worker_controller.retry(cmd.session_id, cmd.worker_id)
        except (WorkerControllerError, RuntimeError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkerRetryResult(
            worker_id=worker.id,
            session_id=worker.session_id,
            status=worker.status.value,
            route_id=worker.route,
            model=worker.model,
            attempt=worker.attempt,
            worktree=worker.worktree,
            read_only=worker.write_claim.read_only,
            backend=worker.backend,
            backend_capabilities=dict(worker.backend_capabilities),
            sandbox_enforcement=worker.sandbox_enforcement,
        )

    # 返回指定 Worker 游标后的有界持久事件，供控制中心增量 peek
    async def _worker_events_handler(self, params: dict[str, Any]) -> WorkerEventsResult:
        assert self._subagent_registry is not None
        cmd = WorkerEventsCommand.model_validate(params)
        self._worker_for_session(cmd.session_id, cmd.worker_id)
        events = self._subagent_registry.events(
            cmd.worker_id,
            after_cursor=cmd.after_cursor,
            limit=cmd.limit,
        )
        return WorkerEventsResult(
            events=[event.model_dump(mode="json") for event in events]
        )

    # 向当前 daemon 内运行中的 Worker 注入后续指令并推进持久事件游标
    async def _worker_followup_handler(
        self,
        params: dict[str, Any],
    ) -> WorkerFollowupResult:
        assert self._subagent_registry is not None
        cmd = WorkerFollowupCommand.model_validate(params)
        current = self._worker_for_session(cmd.session_id, cmd.worker_id)
        try:
            if current.backend != "builtin":
                if not bool(current.backend_capabilities.get("continuation", False)):
                    raise ValueError(
                        f"worker backend does not support continuation: {current.backend}"
                    )
                handle = self._worker_backends.handle(cmd.worker_id)
                if handle is None:
                    raise ValueError("external worker handle is no longer active")
                await handle.followup(cmd.message)
                worker = self._subagent_registry.record_followup(
                    cmd.worker_id,
                    cmd.message,
                )
            elif self._interaction_manager.steer(cmd.worker_id, cmd.message):
                worker = self._subagent_registry.record_followup(
                    cmd.worker_id,
                    cmd.message,
                )
            else:
                worker = self._subagent_registry.followup(cmd.worker_id, cmd.message)
        except (ValueError, WorkerStoreError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkerFollowupResult(
            worker_id=worker.id,
            status=worker.status.value,
            event_cursor=worker.event_cursor,
        )

    # 先返回完整权威补丁供审查，再以同一摘要记录确认结论且不执行 apply 或 merge
    async def _worker_review_handler(self, params: dict[str, Any]) -> WorkerReviewResult:
        assert self._subagent_registry is not None
        cmd = WorkerReviewCommand.model_validate(params)
        try:
            review_digest = ""
            changed_files: list[str] | None = None
            diff_truncated: bool | None = None
            diff_preview = ""
            current = self._worker_for_session(cmd.session_id, cmd.worker_id)
            if cmd.approved:
                preview = await WorktreeManager(
                    WorkspaceBoundary.current().root,
                    self._process_supervisor,
                ).preview_apply(
                    current.worktree,
                    base_commit=current.base_commit,
                )
                review_digest = preview.state_digest
                changed_files = list(preview.changed_files)
                diff_truncated = preview.diff_truncated
                diff_preview = preview.diff
                if not cmd.confirmed:
                    return WorkerReviewResult(
                        worker_id=current.id,
                        handoff_status=current.handoff_status,
                        approved=False,
                        state_digest=review_digest,
                        preview_only=True,
                        changed_files=changed_files,
                        diff=diff_preview,
                        diff_truncated=preview.diff_truncated,
                    )
                if not hmac.compare_digest(cmd.expected_digest, review_digest):
                    raise ValueError(
                        "worker review digest is stale; preview the handoff again"
                    )
            elif not cmd.confirmed:
                raise ValueError("worker rejection requires confirmed=true")
            worker = self._subagent_registry.review_handoff(
                cmd.worker_id,
                approved=cmd.approved,
                review_digest=review_digest,
                changed_files=changed_files,
                diff_truncated=diff_truncated,
                diff_preview=diff_preview if cmd.approved else None,
            )
        except (ValueError, WorkerStoreError, WorktreeError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkerReviewResult(
            worker_id=worker.id,
            handoff_status=worker.handoff_status,
            approved=bool(worker.approved),
            state_digest=worker.review_digest,
            changed_files=list(worker.changed_files),
            diff=worker.diff_preview if cmd.approved else "",
            diff_truncated=worker.diff_truncated,
        )

    # 在全局工作区门闩内重验 Worker 审批与补丁摘要后安全应用到主工作区
    async def _worker_apply_handler(self, params: dict[str, Any]) -> WorkerApplyResult:
        assert self._sessions is not None
        assert self._subagent_registry is not None
        cmd = WorkerApplyCommand.model_validate(params)
        async with self._sessions.workspace_mutation():
            self._require_change_mutation(cmd.session_id, confirmed=cmd.confirmed)
            try:
                worker = self._subagent_registry.require_applicable_handoff(
                    cmd.worker_id,
                    expected_digest=cmd.expected_digest,
                )
                if not worker.session_id or worker.session_id != cmd.session_id:
                    raise ValueError("worker handoff belongs to a different session")
                result = await WorktreeManager(
                    WorkspaceBoundary.current().root,
                    self._process_supervisor,
                ).apply(
                    worker.worktree,
                    base_commit=worker.base_commit,
                    expected_digest=cmd.expected_digest,
                    reviewed_files=tuple(worker.changed_files),
                )
            except WorktreeApplyStateError as exc:
                incident = await self._audit_health.degrade("worker.apply", exc)
                raise HandlerError(
                    INVALID_PARAMS,
                    f"worker apply failed closed; diagnostic_id={incident.diagnostic_id}",
                ) from exc
            except (ValueError, WorkerStoreError, WorktreeError) as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            try:
                applied = self._subagent_registry.mark_handoff_applied(
                    worker.id,
                    state_digest=result.state_digest,
                    changed_files=list(result.changed_files),
                )
            except (ValueError, WorkerStoreError) as exc:
                incident = await self._audit_health.degrade("worker.apply.record", exc)
                raise HandlerError(
                    INVALID_PARAMS,
                    "worker changes were applied but the handoff receipt failed; "
                    f"diagnostic_id={incident.diagnostic_id}",
                ) from exc
        return WorkerApplyResult(
            worker_id=applied.id,
            changed_files=list(result.changed_files),
            state_digest=result.state_digest,
        )

    # 取消持久 Worker 并返回其新状态（先查内存子代理再查 fleet）
    async def _worker_cancel_handler(self, params: dict[str, Any]) -> WorkerCancelResult:
        cmd = WorkerCancelCommand.model_validate(params)
        assert self._subagent_registry is not None
        self._worker_for_session(cmd.session_id, cmd.worker_id, allow_fleet=True)
        if self._subagent_registry.record(cmd.worker_id) is not None:
            worker = await self._subagent_registry.cancel(cmd.worker_id)
            return WorkerCancelResult(
                worker_id=cmd.worker_id, status=worker.status.value
            )
        if (
            self._fleet_registry is not None
            and self._fleet_registry.record(cmd.worker_id) is not None
        ):
            worker = await self._fleet_registry.cancel(cmd.worker_id)
            return WorkerCancelResult(
                worker_id=cmd.worker_id, status=worker.status.value
            )
        raise HandlerError(INVALID_PARAMS, f"worker not found: {cmd.worker_id}")

    # 返回后台 shell 任务列表，或单个任务的全量增量输出
    async def _background_get_handler(self, params: dict[str, Any]) -> BackgroundGetResult:
        cmd = BackgroundGetCommand.model_validate(params)
        records = (
            [self._background_registry.get_for_session(cmd.job_id, cmd.session_id)]
            if cmd.job_id
            else self._background_registry.list(cmd.session_id)
        )
        jobs = [
            BackgroundJobInfo(
                id=job.id,
                command=job.command,
                session_id=job.session_id,
                run_id=job.run_id,
                status=job.status,
                output=job.output,
                is_error=job.is_error,
                created_at=job.created_at,
                finished_at=job.finished_at,
                process_usage=job.process_usage or {},
                output_bytes=job.output_bytes,
                output_truncated=job.output_truncated,
                output_artifact=job.output_artifact,
                output_artifact_size=job.output_artifact_size,
                output_artifact_error=job.output_artifact_error,
            )
            for job in records
            if job is not None
        ]
        return BackgroundGetResult(jobs=jobs)

    # 取消指定后台 shell 任务并返回是否真正终止了运行
    async def _background_cancel_handler(
        self, params: dict[str, Any]
    ) -> BackgroundCancelResult:
        cmd = BackgroundCancelCommand.model_validate(params)
        cancelled = await self._background_registry.cancel(
            cmd.job_id,
            session_id=cmd.session_id,
        )
        return BackgroundCancelResult(job_id=cmd.job_id, cancelled=cancelled)

    # 返回 MCP server 状态与工具清单
    async def _mcp_list_handler(self, params: dict[str, Any]) -> McpListResult:
        McpListCommand.model_validate(params)
        assert self._mcp_manager is not None
        servers = [
            McpServerInfo(
                name=str(state.get("name", "")),
                transport=str(state.get("transport", "")),
                status=str(state.get("status", "")),
                tool_count=len(state.get("tools", [])),
                tools=list(state.get("tools", [])),
                error=str(state.get("error", "")),
            )
            for state in self._mcp_manager.describe()
        ]
        return McpListResult(servers=servers)

    # 返回 hook 配置表与最近执行记录
    async def _hooks_list_handler(self, params: dict[str, Any]) -> HooksListResult:
        self._require_labs("hooks")
        cmd = HooksListCommand.model_validate(params)
        assert self._hooks is not None
        configs = [
            HookConfigInfo(
                id=cfg.id,
                event=cfg.event,
                blocking=cfg.blocking,
                trusted_scope=cfg.trusted_scope,
                on_failure=cfg.on_failure,
                command=list(cfg.command),
                conditions=dict(cfg.conditions),
            )
            for cfg in self._hooks.configs
        ]
        audit_events = [
            HookAuditInfo(
                hook_id=item.hook_id,
                run_id=item.run_id,
                event=item.event,
                status=item.status,
                blocking=item.blocking,
                elapsed_ms=item.elapsed_ms,
                blocked=item.blocked,
                reason=item.reason,
                exit_code=item.exit_code,
                process_usage=item.process_usage,
                ts=item.ts,
            )
            for item in self._hooks.audit_events()[-cmd.limit :]
        ]
        return HooksListResult(configs=configs, audit_events=audit_events)

    # 手动重跑指定 hook，返回本次执行状态
    async def _hook_rerun_handler(self, params: dict[str, Any]) -> HookRerunResult:
        self._require_labs("hooks")
        cmd = HookRerunCommand.model_validate(params)
        assert self._hooks is not None
        audit = await self._hooks.rerun(cmd.hook_id, session_id=cmd.session_id)
        if audit is None:
            raise HandlerError(INVALID_PARAMS, f"hook not found: {cmd.hook_id}")
        return HookRerunResult(
            hook_id=cmd.hook_id,
            executed=audit.status != "skipped_untrusted",
            status=audit.status,
            reason=audit.reason,
            ts=audit.ts,
        )

    # 返回当前 daemon 工作区对应的项目记忆库
    def _memory_store(self) -> MemoryStore:
        return MemoryStore(Path.cwd() / ".coderook" / "memory")

    # 将持久化记忆转换为脱敏且有界的 IPC 记录
    def _memory_info(self, store: MemoryStore, item: MemoryRecord) -> MemoryInfo:
        return MemoryInfo(
            id=item.id,
            name=item.name,
            description=item.description,
            type=item.type,
            body=item.body[:1_000],
            source_session_id=item.source_session_id,
            source_run_id=item.source_run_id,
            created_at=item.created_at,
            updated_at=item.updated_at,
            pinned=item.pinned,
            expires_at=item.expires_at,
            expired=store.is_expired(item),
        )

    # 返回当前项目记忆条目及 Agent 自动保存策略
    async def _memory_list_handler(self, params: dict[str, Any]) -> MemoryListResult:
        cmd = MemoryListCommand.model_validate(params)
        store = self._memory_store()
        settings = store.load_settings()
        return MemoryListResult(
            memories=[
                self._memory_info(store, item)
                for item in store.list_all(include_expired=cmd.include_expired)
            ],
            settings=MemorySettingsInfo(auto_save=settings.auto_save),
        )

    # 手动新增一条项目记忆并返回完整治理元数据
    async def _memory_add_handler(self, params: dict[str, Any]) -> MemoryMutationResult:
        cmd = MemoryAddCommand.model_validate(params)
        store = self._memory_store()
        try:
            record = store.save(
                name=cmd.name,
                description=cmd.description,
                mem_type=cmd.memory_type,
                body=cmd.body,
                source_session_id=cmd.source_session_id,
            )
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return MemoryMutationResult(memory=self._memory_info(store, record))

    # 手动编辑现有项目记忆并保留原始来源
    async def _memory_edit_handler(self, params: dict[str, Any]) -> MemoryMutationResult:
        cmd = MemoryEditCommand.model_validate(params)
        store = self._memory_store()
        try:
            record = store.edit(
                cmd.memory_id,
                name=cmd.name,
                description=cmd.description,
                mem_type=cmd.memory_type,
                body=cmd.body,
            )
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return MemoryMutationResult(memory=self._memory_info(store, record))

    # 固定或取消固定一条项目记忆
    async def _memory_pin_handler(self, params: dict[str, Any]) -> MemoryMutationResult:
        cmd = MemoryPinCommand.model_validate(params)
        store = self._memory_store()
        try:
            record = store.pin(cmd.memory_id, pinned=cmd.pinned)
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return MemoryMutationResult(memory=self._memory_info(store, record))

    # 设置或清除一条项目记忆的过期时间
    async def _memory_expire_handler(self, params: dict[str, Any]) -> MemoryMutationResult:
        cmd = MemoryExpireCommand.model_validate(params)
        store = self._memory_store()
        try:
            record = store.expire(cmd.memory_id, cmd.expires_at)
        except ValueError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return MemoryMutationResult(memory=self._memory_info(store, record))

    # 删除指定记忆并返回是否删除成功
    async def _memory_delete_handler(self, params: dict[str, Any]) -> MemoryDeleteResult:
        cmd = MemoryDeleteCommand.model_validate(params)
        store = self._memory_store()
        deleted = store.forget(cmd.memory_id)
        return MemoryDeleteResult(memory_id=cmd.memory_id, deleted=deleted)

    # 读取 Agent 自动保存记忆的当前策略
    async def _memory_settings_get_handler(
        self,
        params: dict[str, Any],
    ) -> MemorySettingsResult:
        MemorySettingsGetCommand.model_validate(params)
        settings = self._memory_store().load_settings()
        return MemorySettingsResult(
            settings=MemorySettingsInfo(auto_save=settings.auto_save)
        )

    # 更新 Agent 自动保存策略并返回实际落盘值
    async def _memory_settings_set_handler(
        self,
        params: dict[str, Any],
    ) -> MemorySettingsResult:
        cmd = MemorySettingsSetCommand.model_validate(params)
        settings = self._memory_store().set_auto_save(cmd.auto_save)
        return MemorySettingsResult(
            settings=MemorySettingsInfo(auto_save=settings.auto_save)
        )

    # 启动声明式 TOML/JSON workflow，并立即返回 durable workflow ID
    async def _workflow_start_handler(
        self,
        params: dict[str, Any],
    ) -> WorkflowStartResult:
        self._require_labs("workflows")
        assert self._fleet is not None
        cmd = WorkflowStartCommand.model_validate(params)
        try:
            spec = parse_workflow_text(cmd.source, format=cmd.format)
            self._fleet.start(spec)
        except (WorkflowParseError, WorkflowLedgerError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkflowStartResult(workflow_id=spec.id)

    # 列出 durable workflow 元数据供 TUI/CLI projection 使用
    async def _workflow_list_handler(
        self,
        params: dict[str, Any],
    ) -> WorkflowListResult:
        self._require_labs("workflows")
        assert self._fleet is not None
        cmd = WorkflowListCommand.model_validate(params)
        return WorkflowListResult(workflows=self._fleet.list()[-cmd.limit :])

    # 从 SQLite event reducer 返回单个 workflow 的完整 Work Graph
    async def _workflow_get_handler(
        self,
        params: dict[str, Any],
    ) -> WorkflowGetResult:
        self._require_labs("workflows")
        assert self._fleet is not None
        cmd = WorkflowGetCommand.model_validate(params)
        try:
            graph = self._fleet.graph(cmd.workflow_id)
        except WorkflowLedgerError as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkflowGetResult(workflow=graph.model_dump(mode="json"))

    # 返回工作区结构化 Git diff，供 TUI 直接展示文件和补丁
    async def _workspace_diff_handler(self, params: dict[str, Any]) -> WorkspaceDiffResult:
        cmd = WorkspaceDiffCommand.model_validate(params)
        if cmd.path != ".":
            result = await GitDiffTool(WorkspaceBoundary.current()).invoke(
                {"scope": cmd.scope, "path": cmd.path}
            )
            payload = json.loads(result.content)
            return WorkspaceDiffResult(payload=payload)
        payload = await ChangeCenterService(
            WorkspaceBoundary.current(),
            self._process_supervisor,
        ).diff(cmd.scope)
        return WorkspaceDiffResult(payload=payload)

    # 验证 Change Center 写动作的显式确认、会话空闲、审计健康和工作区信任
    def _require_change_mutation(self, session_id: str, *, confirmed: bool) -> None:
        assert self._sessions is not None
        assert self._permission_manager is not None
        self._sessions.get_session(session_id)
        if not confirmed:
            raise HandlerError(INVALID_PARAMS, "change action requires explicit confirmation")
        if self._sessions.is_busy(session_id):
            raise HandlerError(INVALID_PARAMS, "change action is blocked during an active turn")
        if self._sessions.active_run_count() != 0:
            raise HandlerError(
                INVALID_PARAMS,
                "change action is blocked while any workspace turn is active",
            )
        if self._audit_health.degraded:
            raise HandlerError(INVALID_PARAMS, "change action is blocked while audit is degraded")
        authority = self._permission_manager.get_effective_authority_snapshot(session_id)
        if authority.workspace_trust != WorkspaceTrust.TRUSTED:
            raise HandlerError(INVALID_PARAMS, "change action requires a trusted workspace")

    # 把用户明确选择的当前改动加入 Git index，并返回 stage 后的权威 diff
    async def _workspace_stage_handler(self, params: dict[str, Any]) -> WorkspaceStageResult:
        assert self._sessions is not None
        cmd = WorkspaceStageCommand.model_validate(params)
        async with self._sessions.workspace_mutation():
            self._require_change_mutation(cmd.session_id, confirmed=cmd.confirmed)
            service = ChangeCenterService(
                WorkspaceBoundary.current(),
                self._process_supervisor,
            )
            try:
                payload = await service.stage(
                    cmd.paths,
                    expected_digest=cmd.expected_digest,
                )
            except (ChangeCenterError, OSError, ValueError) as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkspaceStageResult(payload=payload)

    # 从已 stage 的审查结果创建本地 commit，禁止自动 push 和隐式仓库 hook
    async def _workspace_commit_handler(self, params: dict[str, Any]) -> WorkspaceCommitResult:
        assert self._sessions is not None
        cmd = WorkspaceCommitCommand.model_validate(params)
        async with self._sessions.workspace_mutation():
            self._require_change_mutation(cmd.session_id, confirmed=cmd.confirmed)
            service = ChangeCenterService(
                WorkspaceBoundary.current(),
                self._process_supervisor,
            )
            try:
                result = await service.commit(
                    cmd.message,
                    expected_digest=cmd.expected_digest,
                )
            except (ChangeCenterError, OSError, ValueError) as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        return WorkspaceCommitResult(
            commit=result.commit,
            subject=result.subject,
            files=list(result.files),
            hooks_skipped=result.hooks_skipped,
        )

    # 返回当前会话指定（默认最近一次）run 的安全恢复点列表
    async def _session_checkpoints_handler(
        self,
        params: dict[str, Any],
    ) -> SessionCheckpointsResult:
        assert self._sessions is not None
        cmd = SessionCheckpointsCommand.model_validate(params)
        run_id, checkpoints = self._sessions.list_checkpoints(cmd.session_id, cmd.run_id)
        return SessionCheckpointsResult(run_id=run_id, checkpoints=checkpoints)

    # 返回 checkpoint 对当前文件的恢复预览和可重验摘要，不执行文件写入
    async def _session_rewind_preview_handler(
        self,
        params: dict[str, Any],
    ) -> SessionRewindPreviewResult:
        assert self._sessions is not None
        cmd = SessionRewindPreviewCommand.model_validate(params)
        preview = self._sessions.preview_rewind(
            cmd.session_id,
            cmd.checkpoint_id,
            cmd.run_id,
        )
        return SessionRewindPreviewResult.model_validate(preview)

    # 恢复用户明确选择的 checkpoint 并返回受影响文件
    async def _session_rewind_handler(
        self,
        params: dict[str, Any],
    ) -> SessionRewindResult:
        assert self._sessions is not None
        cmd = SessionRewindCommand.model_validate(params)
        async with self._sessions.workspace_mutation():
            self._require_change_mutation(cmd.session_id, confirmed=cmd.confirmed)
            result = self._sessions.rewind(
                cmd.session_id,
                cmd.checkpoint_id,
                cmd.run_id,
                expected_digest=cmd.expected_digest,
            )
        return SessionRewindResult.model_validate(result)

    # 返回当前会话的上下文大小和运行概览
    async def _session_context_handler(
        self,
        params: dict[str, Any],
    ) -> SessionContextResult:
        assert self._sessions is not None
        cmd = SessionContextCommand.model_validate(params)
        context = self._sessions.context_info(cmd.session_id)
        last_run_id = context.get("last_run_id")
        if isinstance(last_run_id, str) and self._runtime is not None:
            turn = await self._runtime.get_turn(last_run_id)
            events = [
                event
                for event in await self._runtime.list_events(cmd.session_id)
                if event.turn_id == last_run_id
            ]
            context["usage"] = turn.usage
            for event in reversed(events):
                if event.type == "context.working_set" and not context.get("working_set"):
                    paths = event.payload.get("paths")
                    context["working_set"] = paths if isinstance(paths, list) else []
                elif event.type == "context.compacted" and context.get("compaction") is None:
                    context["compaction"] = event.payload
                elif event.type == "context.budget" and context.get("tool_schema_tokens") is None:
                    context["tool_schema_tokens"] = event.payload.get("tool_schema_tokens")
                    context["system_tokens"] = event.payload.get("system_tokens")
        context["session_usage"] = await self._session_usage_summary(cmd.session_id)
        return SessionContextResult.model_validate(context)

    # 聚合当前 session 的 durable turn 与 Worker 用量，未知单价时只报告已知小计
    async def _session_usage_summary(self, session_id: str) -> dict[str, Any]:
        turns = await self._runtime.list_turns(session_id) if self._runtime is not None else []
        workers_by_id: dict[str, Any] = {}
        for registry in (self._subagent_registry, self._fleet_registry):
            if registry is None:
                continue
            for worker in registry.list_records():
                if worker.session_id == session_id:
                    workers_by_id[worker.id] = worker
        counts = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        models: set[str] = set()
        pricing: list[dict[str, Any]] = []
        known_cost = 0.0
        unknown_cost = False
        for turn in turns:
            usage = turn.usage
            for key in counts:
                value = usage.get(key, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    counts[key] += int(value)
            raw_models = usage.get("models", [])
            if isinstance(raw_models, list):
                for model in raw_models:
                    if isinstance(model, str) and model:
                        models.add(model)
            raw_pricing = usage.get("pricing", [])
            if isinstance(raw_pricing, list):
                for evidence in raw_pricing:
                    if isinstance(evidence, dict) and evidence not in pricing:
                        pricing.append(evidence)
            cost = usage.get("estimated_cost_usd")
            if usage.get("cost_status") == "unknown" or cost == "unknown":
                unknown_cost = True
            elif isinstance(cost, (int, float)) and not isinstance(cost, bool):
                known_cost += float(cost)
        for worker in workers_by_id.values():
            for key in counts:
                counts[key] += int(getattr(worker, key, 0))
            if worker.model:
                models.add(worker.model)
            if worker.cost_status == "unknown":
                unknown_cost = True
            elif worker.estimated_cost_usd is not None:
                known_cost += float(worker.estimated_cost_usd)
        has_usage = any(counts.values())
        return {
            **counts,
            "estimated_cost_usd": "unknown" if unknown_cost else known_cost,
            "known_estimated_cost_usd": known_cost,
            "cost_status": (
                "unknown" if unknown_cost else "estimated" if has_usage else "none"
            ),
            "models": sorted(models),
            "pricing": pricing,
            "turn_count": len(turns),
            "worker_count": len(workers_by_id),
            "worker_token_usage": sum(
                int(worker.token_usage) for worker in workers_by_id.values()
            ),
        }

    # 返回 turn inspector 所需的 durable 状态、items、events 与 receipt
    async def _turn_inspect_handler(self, params: dict[str, Any]) -> TurnInspectResult:
        assert self._runtime is not None
        cmd = TurnInspectCommand.model_validate(params)
        turn = await self._runtime.get_turn(cmd.turn_id)
        items = await self._runtime.list_items(cmd.turn_id)
        events = await self._runtime.list_turn_events(cmd.turn_id)
        receipt = await self._runtime.get_receipt(cmd.turn_id)
        return TurnInspectResult(
            turn=turn.model_dump(mode="json"),
            items=[item.model_dump(mode="json") for item in items],
            events=[event.model_dump(mode="json") for event in events],
            receipt=receipt.model_dump(mode="json"),
        )

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册事件订阅，并为 runtime thread 建立无缝历史回放与实时衔接
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()
        assert self._broadcaster is not None

        if cmd.thread_id is not None:
            return await self._subscribe_runtime_events(cmd, writer)
        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 仅移除当前 IPC writer 自己持有的指定订阅，不暴露或影响其他客户端订阅
    async def _unsubscribe_handler(
        self,
        params: dict[str, Any],
    ) -> EventUnsubscribeResult:
        cmd = EventUnsubscribeCommand.model_validate(params)
        writer = get_connection_writer()
        assert self._broadcaster is not None
        removed = self._broadcaster.unsubscribe(writer, cmd.subscription_id)
        return EventUnsubscribeResult(
            subscription_id=cmd.subscription_id,
            removed=removed,
        )

    # 先注册回放态订阅，再按高水位回放并刷新期间积压的实时事件
    async def _subscribe_runtime_events(
        self,
        cmd: EventSubscribeCommand,
        writer: asyncio.StreamWriter,
    ) -> EventSubscribeResult:
        assert self._runtime is not None
        assert self._broadcaster is not None
        assert cmd.thread_id is not None
        sub_id = self._broadcaster.subscribe(
            writer,
            cmd.topics,
            f"thread:{cmd.thread_id}",
            replaying_runtime=True,
        )
        replayed_count = 0
        cursor = cmd.after_seq
        try:
            high_water = await self._runtime.latest_event_seq(cmd.thread_id)
            while cursor < high_water:
                events = await self._runtime.list_events(
                    cmd.thread_id,
                    after_seq=cursor,
                    up_to_seq=high_water,
                    limit=1000,
                )
                if not events:
                    break
                replayed_count += await self._broadcaster.replay_runtime_batch(
                    sub_id,
                    events,
                )
                if not self._broadcaster.owns_subscription(writer, sub_id):
                    raise ConnectionError("runtime event replay delivery failed")
                cursor = events[-1].seq
            pending_count, cursor = await self._broadcaster.finish_runtime_replay(
                sub_id,
                max(cursor, high_water),
            )
            if not self._broadcaster.owns_subscription(writer, sub_id):
                raise ConnectionError("runtime event handoff delivery failed")
            replayed_count += pending_count
        except Exception:
            self._broadcaster.unsubscribe(writer, sub_id)
            raise
        return EventSubscribeResult(
            subscription_id=sub_id,
            replayed_count=replayed_count,
            last_seq=cursor,
        )

    # 从 events.jsonl 向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        path = events_file(run_id)
        if not path.exists():
            for candidate in Path("~/.coderook/sessions").expanduser().glob(
                f"*/runs/{run_id}/events.jsonl"
            ):
                path = candidate
                break
        if not path.exists():
            return 0

        count = 0
        for line in path.read_text().splitlines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_type: str = event.get("type", "")
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 在统一状态锁内迁移 Provider Catalog，任何凭据或证据损坏均降级为只诊断模式
    async def _initialize_provider_catalog(self, state_root: Path) -> None:
        assert self._config is not None
        self._route_registry = RouteRegistry(self._config.llm)
        migrated_route = None
        try:
            with v1_state_mutation(state_root):
                migrated_route = self._route_registry.migrate_legacy_config(
                    legacy_configured=llm_is_configured(self._config.llm)
                )
        except (
            CredentialStoreError,
            OSError,
            ProviderCatalogMigrationReceiptError,
            RouteResolutionError,
            RouteStoreError,
            StatePathSecurityError,
            UpgradeStateLockError,
            ValueError,
        ) as exc:
            incident = await self._audit_health.degrade("provider.migration", exc)
            logger.error(
                "provider catalog migration failed closed; diagnostic_id=%s",
                incident.diagnostic_id,
            )
        if migrated_route is not None:
            logger.info(
                "migrated legacy LLM configuration to route catalog route=%s",
                migrated_route.id,
            )

    # 把空白 API token 配置收敛为用户级安全凭据并写回本次启动快照
    def _ensure_runtime_api_token(self) -> str:
        assert self._config is not None
        configured = self._config.api.token
        if configured.strip():
            if configured != configured.strip() or any(
                character.isspace() for character in configured
            ):
                raise ValueError("configured Runtime API token contains whitespace")
            return configured
        token = load_or_create_api_token(Path("~/.coderook/api-token").expanduser())
        self._config.api.token = token
        return token

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = (
            get_config()
            if self._env_file is None
            else get_config(env_file=self._env_file)
        )
        require_loopback_host(self._config.host)
        setup_logging(self._config)
        try:
            state_layout = prepare_user_state_layout()
        except StatePathSecurityError as exc:
            raise SystemExit(
                f"unsafe CodeRook user state; run coderook doctor runtime: {exc}"
            ) from None

        if self._daemon_lock is None:
            self._daemon_lock = DaemonLock(state_layout.root / "core.lock")
            try:
                self._daemon_lock.acquire()
            except DaemonLockError as exc:
                raise SystemExit(str(exc)) from None

        ipc_token = load_or_create_ipc_token(
            Path(self._config.ipc_token_file).expanduser()
        )

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(
                trace_path,
                max_bytes=self._config.trace.max_bytes,
                backup_count=self._config.trace.backup_count,
                include_payload=self._config.trace.include_payload,
            )
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        policy_file = state_layout.root / "policy.toml"
        self._permission_manager = PermissionManager(
            policy_file=policy_file,
            timeout_s=self._config.permission.timeout_s,
            audit_health=self._audit_health,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(load_policy_file(policy_file)),
        )
        self._hooks = cast(
            HookManager,
            self._register_workspace_capability(
                CapabilityKind.HOOK_MANAGER,
                "default",
                self._build_hook_manager(Path.cwd()),
                stability=CapabilityStability.LABS,
            ),
        )

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        sessions_root = state_layout.sessions
        store = SessionStore(sessions_root)
        self._goal_service = GoalService(GoalStore(state_layout.goals))
        recovered_goals = self._goal_service.recover_interrupted()
        if recovered_goals:
            logger.info("recovered %d interrupted goals", len(recovered_goals))
        self._bus.subscribe(self._goal_usage_event_handler)
        self._runtime = RuntimeService(
            RuntimeStore(state_layout.runtime_database),
            workspace=Path.cwd(),
            bus=self._bus,
            authority_provider=self._permission_manager.get_authority_snapshot,
            audit_health=self._audit_health,
        )
        self._bus.subscribe(self._runtime.record_bus_event)
        try:
            await self._runtime.recover_stale_turns(datetime.datetime.now(UTC))
        except (OSError, RuntimeStoreError, sqlite3.DatabaseError, ValueError) as exc:
            incident = await self._audit_health.degrade("runtime.recovery", exc)
            logger.error(
                "runtime recovery failed closed; diagnostic_id=%s",
                incident.diagnostic_id,
            )
        await self._initialize_provider_catalog(state_layout.root)
        assert self._route_registry is not None
        self._route_registry = cast(
            RouteRegistry,
            self._register_workspace_capability(
                CapabilityKind.PROVIDER_CATALOG,
                "default",
                self._route_registry,
                stability=CapabilityStability.STABLE,
            ),
        )

        self._mcp_manager = cast(
            McpServerManager,
            self._register_workspace_capability(
                CapabilityKind.MCP_MANAGER,
                "default",
                McpServerManager(
                    self._process_supervisor,
                    enable_labs=self._labs_enabled,
                ),
                stability=CapabilityStability.STABLE,
            ),
        )
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(self._config.mcp.servers)

        # daemon 级 Worker 控制面跨 turn 和重启持久化，内存仅保留本次 boot 任务句柄
        self._subagent_registry = BackgroundTaskRegistry(
            store_path=sessions_root.parent / "workers"
        )
        acp_command = os.environ.get("CODEROOK_ACP_COMMAND", "").strip()
        if self._labs_enabled and acp_command:
            self._worker_backends.register(AcpWorkerBackend(acp_command))
        self._fleet_registry = BackgroundTaskRegistry(
            store=SQLiteWorkerStore(sessions_root.parent / "fleet.db")
        )
        fleet_scheduler = LocalFleetScheduler(
            self._fleet_registry,
            LocalProcessHost(
                (sys.executable, "-m", "code_rook.core.fleet.worker_process"),
                cwd=Path.cwd(),
                process_supervisor=self._process_supervisor,
                sandbox_plan=plan_sandbox(
                    detect_sandbox_capability(),
                    SandboxTier.WORKSPACE_WRITE,
                    str(Path.cwd()),
                    network=True,
                ),
            ),
            workspace=Path.cwd(),
            profiles=self._build_fleet_profiles(),
        )
        self._fleet = LocalFleet(
            WorkflowLedger(sessions_root.parent / "workflow.db"),
            fleet_scheduler,
        )
        self._resume_labs_workflows()

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                background_registry=self._background_registry,
                subagent_registry=self._subagent_registry,
                interaction_manager=self._interaction_manager,
                route_registry=self._route_registry,
                runtime_service=self._runtime,
                hooks=self._hooks,
                process_supervisor=self._process_supervisor,
                persistent_shell_pool=self._persistent_shell_pool,
                goal_service=self._goal_service,
                audit_health=self._audit_health,
                capability_kernel=self._capability_kernel,
            ),
            bus=self._bus,
            subagent_registry=self._subagent_registry,
            runtime_service=self._runtime,
            interaction_manager=self._interaction_manager,
            route_registry=self._route_registry,
            hooks=self._hooks,
            goal_service=self._goal_service,
            authority_provider=self._permission_manager.get_authority_snapshot,
        )
        self._worker_controller = WorkerController(
            registry=self._subagent_registry,
            sessions=self._sessions,
            session_store=store,
            route_registry=self._route_registry,
            permission_manager=self._permission_manager,
            bus=self._bus,
            workspace_boundary=WorkspaceBoundary.current(),
            max_steps=self._config.agent.max_steps,
            hooks=self._hooks,
            interaction_manager=self._interaction_manager,
            goal_service=self._goal_service,
            backend_registry=self._worker_backends,
        )
        self._runtime_api = RuntimeApiService(
            self._runtime,
            self._sessions,
            permission_manager=self._permission_manager,
            workspace_boundary=WorkspaceBoundary.current(),
            labs_enabled=self._labs_enabled,
        )
        self._bus.subscribe(self._runtime_api.notify_runtime_event)
        api_token = self._ensure_runtime_api_token()
        self._http_api = HttpApiServer(
            self._config.api.host,
            self._config.api.port,
            api_token,
            self._runtime_api,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
            auth_token=ipc_token,
        )
        server.register("core.ping", self._ping_handler)
        server.register("core.shutdown", self._shutdown_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("goal.create", self._goal_create_handler)
        server.register("goal.get", self._goal_get_handler)
        server.register("goal.list", self._goal_list_handler)
        server.register("goal.edit", self._goal_edit_handler)
        server.register("goal.pause", self._goal_pause_handler)
        server.register("goal.resume", self._goal_resume_handler)
        server.register("goal.clear", self._goal_clear_handler)
        server.register("goal.complete", self._goal_complete_handler)
        server.register("goal.continue_decision", self._goal_continue_decision_handler)
        server.register("run.cancel", self._run_cancel_handler)
        server.register("run.steer", self._run_steer_handler)
        server.register("artifact.list", self._artifact_list_handler)
        server.register("artifact.gc", self._artifact_gc_handler)
        server.register("event.replay", self._event_replay_handler)
        server.register("thread.create", self._thread_create_handler)
        server.register("thread.list", self._thread_list_handler)
        server.register("thread.get", self._thread_get_handler)
        server.register("thread.update", self._thread_update_handler)
        server.register("thread.archive", self._thread_archive_handler)
        server.register("turn.start", self._turn_start_handler)
        server.register("turn.get", self._turn_get_handler)
        server.register("turn.list", self._turn_list_handler)
        server.register("turn.interrupt", self._turn_interrupt_handler)
        server.register("turn.steer", self._turn_steer_handler)
        server.register("turn.items", self._turn_items_handler)
        server.register("runtime.capabilities", self._runtime_capabilities_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("event.unsubscribe", self._unsubscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_authority", self._session_get_authority_handler)
        server.register("session.set_authority", self._session_set_authority_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.list", self._session_list_handler)
        server.register("session.resume", self._session_resume_handler)
        server.register("session.rename", self._session_rename_handler)
        server.register("session.fork", self._session_fork_handler)
        server.register("session.export", self._session_export_handler)
        server.register("session.delete", self._session_delete_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("user_question.respond", self._user_question_respond_handler)
        server.register("plan.respond", self._plan_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("session.tasks", self._session_tasks_handler)
        server.register("worker.start", self._worker_start_handler)
        server.register("worker.status", self._worker_status_handler)
        server.register("worker.retry", self._worker_retry_handler)
        server.register("worker.list", self._worker_list_handler)
        server.register("worker.events", self._worker_events_handler)
        server.register("worker.followup", self._worker_followup_handler)
        server.register("worker.review", self._worker_review_handler)
        server.register("worker.apply", self._worker_apply_handler)
        server.register("workflow.start", self._workflow_start_handler)
        server.register("workflow.list", self._workflow_list_handler)
        server.register("workflow.get", self._workflow_get_handler)
        server.register("workspace.diff", self._workspace_diff_handler)
        server.register("workspace.stage", self._workspace_stage_handler)
        server.register("workspace.commit", self._workspace_commit_handler)
        server.register("session.checkpoints", self._session_checkpoints_handler)
        server.register("session.rewind_preview", self._session_rewind_preview_handler)
        server.register("session.rewind", self._session_rewind_handler)
        server.register("session.context", self._session_context_handler)
        server.register("turn.inspect", self._turn_inspect_handler)
        server.register("mcp.list", self._mcp_list_handler)
        server.register("hooks.list", self._hooks_list_handler)
        server.register("hooks.rerun", self._hook_rerun_handler)
        server.register("memory.list", self._memory_list_handler)
        server.register("memory.add", self._memory_add_handler)
        server.register("memory.edit", self._memory_edit_handler)
        server.register("memory.pin", self._memory_pin_handler)
        server.register("memory.expire", self._memory_expire_handler)
        server.register("memory.delete", self._memory_delete_handler)
        server.register("memory.settings.get", self._memory_settings_get_handler)
        server.register("memory.settings.set", self._memory_settings_set_handler)
        server.register("background.get", self._background_get_handler)
        server.register("background.cancel", self._background_cancel_handler)
        server.register("worker.cancel", self._worker_cancel_handler)

        addr = await server.start()
        try:
            api_addr = await self._http_api.start()
        except Exception:
            await server.stop()
            raise
        logger.info("coderook-core %s listening addr=%s", code_rook.__version__, addr)
        logger.info("runtime API listening addr=%s", api_addr)
        logger.info("config: %s", self._config)

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        self._shutdown_event = shutdown
        try:
            loop.add_signal_handler(signal.SIGINT, shutdown.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown.set)
        except NotImplementedError:
            logger.warning("signal handlers are not supported by this event loop")

        await shutdown.wait()

        logger.info("shutting down")
        if self._http_api is not None:
            await self._http_api.stop()
        if self._fleet is not None:
            await self._fleet.shutdown()
        if self._subagent_registry is not None:
            self._subagent_registry.begin_shutdown()
        if self._sessions is not None:
            await self._sessions.cancel_all()
        if self._runtime_api is not None:
            await self._runtime_api.close()
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        if self._runtime is not None:
            await self._runtime.drain_pending_writes()
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await self._background_registry.cancel_all()
        if self._subagent_registry is not None:
            await self._subagent_registry.cancel_all()
        await self._worker_backends.close()
        if self._hooks is not None:
            await self._hooks.close()
        await self._persistent_shell_pool.aclose_all()
        await self._process_supervisor.close()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()
        self._capability_kernel.dispose_scope(self._capability_scope)
        if self._daemon_lock is not None:
            self._daemon_lock.release()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    parser = argparse.ArgumentParser(prog="coderook-core", description="CodeRook Core")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Explicit environment file; repository .env files are never loaded automatically",
    )
    args = parser.parse_args()
    try:
        state_layout = prepare_user_state_layout()
    except StatePathSecurityError as exc:
        raise SystemExit(
            f"unsafe CodeRook user state; run coderook doctor runtime: {exc}"
        ) from None
    daemon_lock = DaemonLock(state_layout.root / "core.lock")
    try:
        daemon_lock.acquire()
    except DaemonLockError as exc:
        raise SystemExit(str(exc)) from None
    try:
        ensure_v1_upgrade_backup()
        migrate_legacy_state()
        app = CoreApp(env_file=args.env_file)
        app._daemon_lock = daemon_lock
        asyncio.run(app.run())
    finally:
        daemon_lock.release()

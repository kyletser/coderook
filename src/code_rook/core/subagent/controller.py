from __future__ import annotations

import asyncio
from pathlib import Path

from code_rook.core.authority import AuthoritySnapshot, narrow_child_authority
from code_rook.core.bus.commands import WorkerStartCommand
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.features import labs_enabled
from code_rook.core.goal import GoalBudgetProvider, GoalRecord, GoalService, GoalStoreError
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.factory import create_provider_for_route
from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runs import new_run_id
from code_rook.core.session import Session, SessionManager, SessionStore
from code_rook.core.subagent.backends import (
    WorkerBackendRegistry,
    WorkerHandle,
    WorkerLaunchSpec,
)
from code_rook.core.subagent.models import WorkerRecord, WorkerStatus, WriteClaim
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.tool import SpawnAgentTool
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.core.worktree import WorktreeManager


class WorkerControllerError(ValueError):
    pass


# 由 daemon 持有 Worker 启动依赖，确保 IPC 不借用会话消息伪装控制动作
class WorkerController:
    # 绑定持久 registry、会话、路由、权限和受管工作区依赖
    def __init__(
        self,
        *,
        registry: BackgroundTaskRegistry,
        sessions: SessionManager,
        session_store: SessionStore,
        route_registry: RouteRegistry,
        permission_manager: PermissionManager,
        bus: EventBus,
        workspace_boundary: WorkspaceBoundary,
        max_steps: int,
        hooks: HookManager | None = None,
        interaction_manager: InteractionManager | None = None,
        goal_service: GoalService | None = None,
        backend_registry: WorkerBackendRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._sessions = sessions
        self._session_store = session_store
        self._route_registry = route_registry
        self._permission_manager = permission_manager
        self._bus = bus
        self._boundary = workspace_boundary
        self._max_steps = max_steps
        self._hooks = hooks
        self._interaction_manager = interaction_manager
        self._goal_service = goal_service
        self._backend_registry = backend_registry or WorkerBackendRegistry()
        self._worktrees = WorktreeManager(workspace_boundary.root)

    # 要求父会话真实存在、属于当前工作区且尚未关闭
    def _require_session(self, session_id: str) -> Session:
        try:
            session = self._sessions.get_session(session_id)
        except HandlerError as exc:
            raise WorkerControllerError("parent session is unavailable") from exc
        if session.status == "closed":
            raise WorkerControllerError("parent session is closed")
        try:
            same_workspace = Path(session.workspace).resolve() == self._boundary.root
        except (OSError, RuntimeError) as exc:
            raise WorkerControllerError("parent session workspace is invalid") from exc
        if not same_workspace:
            raise WorkerControllerError("parent session belongs to another workspace")
        return session

    # 将当前会话权限与可选 Goal ceiling 取交集并立即冻结
    def _frozen_authority(self, session_id: str, goal: GoalRecord | None) -> AuthoritySnapshot:
        current = self._permission_manager.get_effective_authority_snapshot(session_id)
        if goal is None:
            return current
        return narrow_child_authority(
            current,
            profile_ceiling=goal.permission_ceiling.profile,
            allowed_actions=(
                current.allowed_actions & goal.permission_ceiling.allowed_actions
            ),
            requested_mode=goal.permission_ceiling.mode,
            requested_trust=goal.permission_ceiling.workspace_trust,
        )

    # 解析新 Worker 应绑定的活动 Goal，非 active Goal 不被后台执行静默续跑
    def _current_goal(self, session_id: str) -> GoalRecord | None:
        if self._goal_service is None:
            return None
        goal = self._goal_service.current(session_id)
        return goal if goal is not None and goal.status == "active" else None

    # 解析重试记录原 Goal，重启后暂停或已终结状态必须由用户先恢复 Goal
    def _retry_goal(self, worker: WorkerRecord) -> GoalRecord | None:
        if self._goal_service is None or not worker.root_goal_id.startswith("goal-"):
            return None
        try:
            goal = self._goal_service.get(worker.root_goal_id)
        except GoalStoreError as exc:
            raise WorkerControllerError("original worker goal is unavailable") from exc
        if goal.session_id != worker.session_id:
            raise WorkerControllerError("original worker goal belongs to another session")
        if goal.status != "active" or goal.paused_needs_confirmation:
            raise WorkerControllerError(
                "original worker goal is not active; resume it before retry"
            )
        return goal

    # 创建绑定统一 Route Catalog 和可选 Goal 硬预算的 provider
    def _provider(
        self,
        route: ResolvedRoute,
        goal: GoalRecord | None,
    ) -> LLMProvider:
        provider = create_provider_for_route(route.route, route.credential)
        if goal is not None and self._goal_service is not None:
            return GoalBudgetProvider(provider, self._goal_service, goal.id)
        return provider

    # 使用 SpawnAgentTool 的同一受管 worktree/claim 管线真正启动后台 Worker
    async def _launch(
        self,
        *,
        session_id: str,
        route: ResolvedRoute,
        authority: AuthoritySnapshot,
        goal: GoalRecord | None,
        worker_id: str,
        params: dict[str, object],
        root_goal_id: str,
    ) -> WorkerRecord:
        parent_control_id = new_run_id()
        tool = SpawnAgentTool(
            provider=self._provider(route, goal),
            parent_bus=self._bus,
            parent_run_id=parent_control_id,
            permission_manager=self._permission_manager,
            max_steps=self._max_steps,
            task_registry=self._registry,
            runs_dir=self._session_store.runs_dir(session_id),
            session_id=session_id,
            workspace_boundary=self._boundary,
            route_registry=self._route_registry,
            hooks=self._hooks,
            interaction_manager=self._interaction_manager,
            authority_ceiling=authority,
            route_binding=route,
            forced_worker_id=worker_id,
            root_goal_id=root_goal_id,
            require_route_readiness=True,
        )
        result = await tool.invoke(params)
        if result.is_error:
            raise WorkerControllerError(result.content)
        worker = self._registry.record(worker_id)
        if worker is None:
            raise WorkerControllerError("worker launch did not produce a durable record")
        if worker.session_id != session_id:
            raise WorkerControllerError("worker launch produced a cross-session record")
        return worker

    # 从显式用户参数启动新的持久 Worker，写入型任务默认进入独立 worktree
    async def start(self, command: WorkerStartCommand) -> WorkerRecord:
        self._require_session(command.session_id)
        if not command.read_only and not (
            command.exact_files
            or command.write_roots
            or command.coordination_contract.strip()
        ):
            raise WorkerControllerError(
                "writing worker requires exact_files, write_roots, or coordination_contract"
            )
        if command.backend != "builtin":
            return await self._start_external(command)
        route = await self._route_registry.resolve_ready(
            command.route_id or None,
            model=command.model or None,
        )
        goal = self._current_goal(command.session_id)
        if (
            goal is not None
            and command.token_budget is not None
            and goal.token_budget is not None
            and command.token_budget != goal.token_budget
        ):
            raise WorkerControllerError("worker budget conflicts with active Goal budget")
        token_budget = goal.token_budget if goal and goal.token_budget else command.token_budget
        worker_id = new_run_id()
        return await self._launch(
            session_id=command.session_id,
            route=route,
            authority=self._frozen_authority(command.session_id, goal),
            goal=goal,
            worker_id=worker_id,
            root_goal_id=goal.id if goal is not None else worker_id,
            params={
                "description": command.description,
                "prompt": command.prompt,
                "run_in_background": True,
                "subagent_type": command.profile,
                "read_only": command.read_only,
                "exact_files": list(command.exact_files),
                "write_roots": list(command.write_roots),
                "coordination_contract": command.coordination_contract,
                "acceptance": list(command.acceptance),
                "token_budget": token_budget,
                "wall_time_s": command.wall_time_s,
                "max_attempts": command.max_attempts,
                "retry_backoff_s": command.retry_backoff_s,
            },
        )

    # 在独立 Worktree 内启动一个能力显式且默认标记 partial enforcement 的外部 Worker
    async def _start_external(self, command: WorkerStartCommand) -> WorkerRecord:
        if not labs_enabled():
            raise WorkerControllerError(
                "external worker backends require CODEROOK_LABS=1"
            )
        backend = self._backend_registry.require(command.backend)
        worker_id = new_run_id()
        base_commit = await self._worktrees.resolve_ref()
        worktree_path = await self._worktrees.create(worker_id, base_commit)
        goal = self._current_goal(command.session_id)
        claim = WriteClaim(
            read_only=command.read_only,
            exact_files=list(command.exact_files),
            write_roots=list(command.write_roots),
            coordination_contract=command.coordination_contract,
        )
        worker = self._registry.new_record(
            worker_id=worker_id,
            parent_turn_id=new_run_id(),
            root_goal_id=goal.id if goal is not None else worker_id,
            description=command.description,
            prompt=command.prompt,
            workspace=str(self._boundary.root),
            authority_ceiling=self._frozen_authority(command.session_id, goal),
            depth=1,
            max_steps=self._max_steps,
            session_id=command.session_id,
            profile=command.profile,
            worktree=worker_id,
            branch=f"coderook/{worker_id}",
            base_commit=base_commit,
            merge_owner="user" if not command.read_only else "",
            merge_reviewer="user" if not command.read_only else "",
            write_claim=claim,
            acceptance=list(command.acceptance),
            wall_time_s=command.wall_time_s,
            token_budget=command.token_budget,
            max_attempts=command.max_attempts,
            retry_backoff_s=command.retry_backoff_s,
            backend=command.backend,
            backend_capabilities=backend.capabilities.model_dump(mode="json"),
            sandbox_enforcement="partial",
        )
        self._registry.create(worker)
        try:
            handle = await self._backend_registry.start(
                command.backend,
                WorkerLaunchSpec(
                    worker_id=worker_id,
                    prompt=command.prompt,
                    cwd=worktree_path,
                    read_only=command.read_only,
                    env={},
                ),
            )
        except Exception as exc:
            self._registry.update_status(
                worker_id,
                WorkerStatus.FAILED,
                reason=f"backend_start_failed:{type(exc).__name__}",
            )
            raise WorkerControllerError(
                f"external worker failed to start: {type(exc).__name__}"
            ) from exc
        context = ExecutionContext(
            run_id=worker_id,
            goal=command.prompt,
            max_steps=self._max_steps,
        )
        task = asyncio.create_task(
            self._monitor_external(worker_id, handle, command.read_only)
        )
        self._registry.register(
            worker_id,
            task,
            context,
            parent_run_id=worker.parent_turn_id,
        )
        self._registry.append_event(
            worker_id,
            "worker.backend_started",
            f"backend={command.backend} enforcement=partial",
        )
        current = self._registry.record(worker_id)
        if current is None:
            raise WorkerControllerError("external worker record disappeared after start")
        return current

    # 等待外部 Backend 终态并把 Worktree 结果收口到现有审查和应用契约
    async def _monitor_external(
        self,
        worker_id: str,
        handle: WorkerHandle,
        read_only: bool,
    ) -> None:
        try:
            result = await handle.result()
            worker = self._registry.record(worker_id)
            if worker is None:
                return
            inspection = await self._worktrees.inspect(
                worker_id,
                base_commit=worker.base_commit,
            )
            if read_only and inspection.changed_files:
                self._registry.update_status(
                    worker_id,
                    WorkerStatus.FAILED,
                    reason="external_read_only_violation",
                    blockers=list(inspection.changed_files),
                    changed_files=list(inspection.changed_files),
                    diff_stat=inspection.diff_stat,
                    diff_preview=inspection.diff,
                    diff_truncated=inspection.diff_truncated,
                )
                return
            status = (
                WorkerStatus.COMPLETED
                if result.status == "completed"
                else WorkerStatus.CANCELLED
                if result.status == "cancelled"
                else WorkerStatus.FAILED
            )
            self._registry.update_status(
                worker_id,
                status,
                reason=result.diagnostic,
                summary=result.output[:4_000],
                changed_files=list(inspection.changed_files),
                diff_stat=inspection.diff_stat,
                diff_preview=inspection.diff,
                diff_truncated=inspection.diff_truncated,
                handoff_status=(
                    "read_only"
                    if read_only
                    else "pending_review"
                    if inspection.changed_files
                    else "no_changes"
                ),
            )
            for event in handle.events():
                self._registry.append_event(
                    worker_id,
                    "worker.backend_event",
                    str(event.get("type") or event.get("method") or "event")[:200],
                )
        except asyncio.CancelledError:
            await handle.cancel()
            self._registry.update_status(
                worker_id,
                WorkerStatus.CANCELLED,
                reason="cancelled",
            )
            raise
        except Exception as exc:
            self._registry.update_status(
                worker_id,
                WorkerStatus.FAILED,
                reason=f"backend_runtime_failed:{type(exc).__name__}",
            )
        finally:
            await self._backend_registry.dispose_handle(worker_id)

    # 按原记录的 route、角色、预算、worktree、claim 和 authority ceiling 重启一次 attempt
    async def retry(self, session_id: str, worker_id: str) -> WorkerRecord:
        self._require_session(session_id)
        worker = self._registry.record(worker_id)
        if worker is None:
            raise WorkerControllerError(f"worker not found: {worker_id}")
        if not worker.session_id or worker.session_id != session_id:
            raise WorkerControllerError("worker belongs to a different session")
        if worker.backend != "builtin":
            return await self._retry_external(worker)
        if not worker.route or not worker.route_digest:
            raise WorkerControllerError(
                "legacy worker has no immutable route receipt and cannot be retried"
            )
        route = await self._route_registry.resolve_ready(
            worker.route,
            model=worker.model,
            expected_digest=worker.route_digest,
        )
        goal = self._retry_goal(worker)
        return await self._launch(
            session_id=session_id,
            route=route,
            authority=self._frozen_authority(session_id, goal),
            goal=goal,
            worker_id=worker.id,
            root_goal_id=worker.root_goal_id,
            params={
                "description": worker.description,
                "prompt": worker.prompt,
                "worker_id": worker.id,
                "run_in_background": True,
            },
        )

    # 在原受管 Worktree 无残留改动时以同一外部 Backend 启动下一 attempt
    async def _retry_external(self, worker: WorkerRecord) -> WorkerRecord:
        if not labs_enabled():
            raise WorkerControllerError(
                "external worker backends require CODEROOK_LABS=1"
            )
        self._backend_registry.require(worker.backend)
        inspection = await self._worktrees.inspect(
            worker.worktree,
            base_commit=worker.base_commit,
        )
        if inspection.changed_files:
            raise WorkerControllerError(
                "external worker has unreviewed worktree changes and cannot be retried"
            )
        try:
            prepared = self._registry.prepare_retry(worker.id)
            handle = await self._backend_registry.start(
                prepared.backend,
                WorkerLaunchSpec(
                    worker_id=prepared.id,
                    prompt=prepared.prompt,
                    cwd=self._worktrees.path_for(prepared.worktree),
                    read_only=prepared.write_claim.read_only,
                    env={},
                ),
            )
        except Exception as exc:
            self._registry.update_status(
                worker.id,
                WorkerStatus.FAILED,
                reason=f"backend_retry_failed:{type(exc).__name__}",
            )
            raise WorkerControllerError(
                f"external worker failed to retry: {type(exc).__name__}"
            ) from exc
        context = ExecutionContext(
            run_id=prepared.id,
            goal=prepared.prompt,
            max_steps=prepared.max_steps,
        )
        task = asyncio.create_task(
            self._monitor_external(
                prepared.id,
                handle,
                prepared.write_claim.read_only,
            )
        )
        self._registry.register(
            prepared.id,
            task,
            context,
            parent_run_id=prepared.parent_turn_id,
        )
        self._registry.append_event(
            prepared.id,
            "worker.backend_retried",
            f"backend={prepared.backend} attempt={prepared.attempt}",
        )
        current = self._registry.record(prepared.id)
        if current is None:
            raise WorkerControllerError("external worker record disappeared after retry")
        return current

    # 返回严格绑定父 session 的单个 WorkerRecord
    def status(self, session_id: str, worker_id: str) -> WorkerRecord:
        self._require_session(session_id)
        worker = self._registry.record(worker_id)
        if worker is None:
            raise WorkerControllerError(f"worker not found: {worker_id}")
        if not worker.session_id or worker.session_id != session_id:
            raise WorkerControllerError("worker belongs to a different session")
        return worker

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from code_rook.core.artifacts import (
    ArtifactError,
    ArtifactStore,
    ImageArtifactInput,
    inspect_image,
)
from code_rook.core.authority import AuthoritySnapshot, RuntimeMode, WorkspaceTrust
from code_rook.core.bus.envelope import INVALID_PARAMS, HandlerError
from code_rook.core.bus.events import (
    GoalContinueDecisionEvent,
    PlanReadyEvent,
    PlanResolvedEvent,
    RecoveryAvailableEvent,
    RecoveryResolvedEvent,
    RunSteeredEvent,
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionDeletedEvent,
    SessionForkedEvent,
    SessionInterruptedEvent,
    SessionMessageReceivedEvent,
    SessionRenamedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from code_rook.core.capabilities import CapabilityStability
from code_rook.core.checkpoints import CheckpointError, CheckpointStore
from code_rook.core.compact.protocol import estimate_messages_tokens
from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.route_registry import RouteResolutionError
from code_rook.core.memory import MemoryStore
from code_rook.core.presets import get_agent_preset
from code_rook.core.runs import new_run_id
from code_rook.core.runtime.models import QueuedMessageRecord, TurnStatus
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import QueuedMessageDispatchingError
from code_rook.core.session.exporter import SessionExportFormat, export_session
from code_rook.core.session.model import Session, SessionMode
from code_rook.core.session.store import SessionStore
from code_rook.core.skills.loader import SkillError, SkillLoader
from code_rook.core.skills.models import Skill
from code_rook.core.task.model import Task
from code_rook.core.workspace import WorkspaceBoundary

if TYPE_CHECKING:
    from code_rook.core.goal import GoalContinueDecision, GoalRecord, GoalService
    from code_rook.core.llm.base import LLMProvider
    from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
    from code_rook.core.runner import AgentRunner
    from code_rook.core.subagent.registry import BackgroundTaskRegistry

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_NOT_RESUMABLE = -32013
RUN_NOT_ACTIVE = -32014
_TRANSIENT_GOAL_FAILURES = {
    "stream_idle_timeout",
    "stream_wall_timeout",
    "token_budget_reserved",
    "transport_error",
}
_AUTO_GOAL_PROMPT = (
    "Continue the active durable goal. Work only on unmet completion criteria, "
    "reuse existing evidence, verify every change, and request confirmation when "
    "the bounded continuation policy pauses."
)
_EMPTY_SESSION_RETENTION = timedelta(hours=24)
logger = logging.getLogger(__name__)


@dataclass
class _ActiveRun:
    session_id: str
    task: asyncio.Task[Any]
    finished: asyncio.Event


@dataclass
class _GoalContinuation:
    session_id: str
    task: asyncio.Task[None]


@dataclass(frozen=True)
class _PendingPlan:
    session_id: str
    run_id: str
    request: str
    plan: str
    plan_ticket: str = ""


class WorkspaceMutationGuard:
    # 初始化允许并发 Turn、但让工作区变更独占且等待活动 Turn 排空的异步门闩
    def __init__(self, entry_lock: asyncio.Lock | None = None) -> None:
        self._entry_lock = entry_lock or asyncio.Lock()
        self._idle = asyncio.Event()
        self._idle.set()
        self._active_turns = 0

    # 为一次 Turn 登记共享占用，变更开始后禁止新 Turn 穿透
    @asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        async with self._entry_lock:
            self._active_turns += 1
            self._idle.clear()
        try:
            yield
        finally:
            self._active_turns -= 1
            if self._active_turns == 0:
                self._idle.set()

    # 独占工作区变更窗口并等待所有已登记 Turn 完成
    @asynccontextmanager
    async def mutation(self) -> AsyncIterator[None]:
        async with self._entry_lock:
            await self._idle.wait()
            yield


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入文件存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
        subagent_registry: BackgroundTaskRegistry | None = None,
        runtime_service: RuntimeService | None = None,
        interaction_manager: InteractionManager | None = None,
        route_registry: RouteRegistry | None = None,
        hooks: HookManager | None = None,
        goal_service: GoalService | None = None,
        authority_provider: Callable[[str], AuthoritySnapshot] | None = None,
        workspace_mutation_guard: WorkspaceMutationGuard | None = None,
        workspace_mutation_lock: asyncio.Lock | None = None,
    ) -> None:
        if workspace_mutation_guard is not None and workspace_mutation_lock is not None:
            raise ValueError(
                "workspace_mutation_guard and workspace_mutation_lock are mutually exclusive"
            )
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._subagent_registry = subagent_registry
        self._runtime = runtime_service
        self._interaction_manager = interaction_manager
        self._route_registry = route_registry
        self._hooks = hooks
        self._goal_service = goal_service
        self._authority_provider = authority_provider
        self._workspace_mutation_guard = workspace_mutation_guard or WorkspaceMutationGuard(
            workspace_mutation_lock
        )
        self._runtime_bootstrapped = False
        self._runtime_bootstrap_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_runs: dict[str, _ActiveRun] = {}
        self._goal_continuations: dict[str, _GoalContinuation] = {}
        self._queue_dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._queue_wakeups: dict[str, asyncio.Event] = {}
        self._queue_recovered = False
        self._queue_closing = False
        self._pending_plans: dict[str, _PendingPlan] = {}
        self._pending_plans_loaded: set[str] = set()
        self._pending_recoveries: set[str] = set()
        workspace = WorkspaceBoundary.current().root
        self._workspace = workspace.resolve()
        self._skill_loader = SkillLoader(self._workspace)
        self._artifact_store = ArtifactStore(workspace / ".coderook" / "artifacts")
        self._rehydrate()

    # 将升级后发生摘要漂移的稳定 Preset 自动迁移到当前定义，避免历史会话无法继续使用
    def _refresh_stable_preset(self, session: Session) -> None:
        preset = get_agent_preset(session.preset_id)
        if session.preset_digest == preset.digest:
            return
        if preset.stability != CapabilityStability.STABLE:
            raise HandlerError(
                INVALID_PARAMS,
                "experimental session preset changed; fork the session before continuing",
            )
        previous_digest = session.preset_digest
        session.preset_digest = preset.digest
        session.updated_at = _now()
        self._store.write_meta(session)
        self._store.append_session_event(
            session.id,
            event_type="session.preset_migrated",
            payload={
                "preset_id": preset.id,
                "previous_digest": previous_digest,
                "preset_digest": preset.digest,
            },
        )
        logger.info(
            "migrated stable session preset sid=%s preset=%s",
            session.id,
            preset.id,
        )

    # 完成 Goal run 后持久化有限继续决策并发布可回放的 typed 事件
    async def _finish_goal_run(
        self,
        goal_id: str,
        run_id: str,
        *,
        succeeded: bool,
        reason: str,
    ) -> GoalContinueDecision | None:
        if self._goal_service is None:
            return None
        transient_failure = not succeeded and reason in _TRANSIENT_GOAL_FAILURES
        goal = self._goal_service.finish_run(
            goal_id,
            run_id,
            succeeded=succeeded,
            reason=reason,
            transient_failure=transient_failure,
        )
        if not goal.auto_continue:
            return None
        authority = (
            self._authority_provider(goal.session_id)
            if self._authority_provider is not None
            else None
        )
        decision = self._goal_service.decide_continue(
            goal.id,
            current_authority=authority,
        )
        await self._bus.publish(
            GoalContinueDecisionEvent(
                goal_id=decision.goal_id,
                session_id=decision.session_id,
                run_id=run_id,
                should_continue=decision.should_continue,
                reason=decision.reason,
                auto_turns_used=decision.auto_turns_used,
                remaining_auto_turns=decision.remaining_auto_turns,
                tokens_used=decision.tokens_used,
                token_budget=decision.token_budget,
                remaining_tokens=decision.remaining_tokens,
                wall_elapsed_seconds=decision.wall_elapsed_seconds,
                max_wall_seconds=decision.max_wall_seconds,
                paused_needs_confirmation=decision.paused_needs_confirmation,
                ts=decision.decided_at,
            )
        )
        return decision

    # 将允许的下一 Goal Turn 排入 daemon 生命周期，并在会话锁释放后启动
    def _schedule_goal_continuation(
        self,
        goal_id: str,
        session_id: str,
        *,
        delay_s: float = 0.0,
    ) -> None:
        existing = self._goal_continuations.get(goal_id)
        if existing is not None and not existing.task.done():
            return

        # 重新读取 Goal 真值后启动下一轮，用户在间隙执行 pause/cancel 会使任务安全退出
        async def continue_goal() -> None:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            while self._goal_service is not None:
                goal = self._goal_service.get(goal_id)
                if (
                    goal.status != "active"
                    or not goal.auto_continue
                    or goal.paused_needs_confirmation
                ):
                    return
                authority = (
                    self._authority_provider(session_id)
                    if self._authority_provider is not None
                    else None
                )
                decision = self._goal_service.decide_continue(
                    goal_id,
                    current_authority=authority,
                )
                if decision.reason == "token_budget_reserved":
                    await asyncio.sleep(0.1)
                    continue
                if not decision.should_continue:
                    return
                await self.send_message(
                    session_id,
                    _AUTO_GOAL_PROMPT,
                    runtime_mode=goal.permission_ceiling.mode,
                )
                return

        task = asyncio.create_task(
            continue_goal(),
            name=f"goal-continuation:{goal_id}",
        )
        self._goal_continuations[goal_id] = _GoalContinuation(
            session_id=session_id,
            task=task,
        )

        # 清理已终结任务，并把意外调度故障安全地转为 Goal blocked 状态
        def cleanup(completed: asyncio.Task[None]) -> None:
            current = self._goal_continuations.get(goal_id)
            if current is not None and current.task is completed:
                self._goal_continuations.pop(goal_id, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is None:
                return
            logger.error(
                "automatic goal continuation failed goal_id=%s error_type=%s",
                goal_id,
                type(error).__name__,
            )
            if self._goal_service is None:
                return
            try:
                goal = self._goal_service.get(goal_id)
                if goal.status == "active":
                    self._goal_service.set_status(
                        goal_id,
                        "blocked",
                        reason="automatic continuation dispatch failed",
                        actor="system",
                    )
            except (ValueError, OSError):
                logger.exception(
                    "could not persist automatic continuation failure goal_id=%s",
                    goal_id,
                )

        task.add_done_callback(cleanup)

    # 校验图片 artifact 元数据并构造只用于下一次模型请求的内存图片块
    async def _prepare_image_attachments(
        self,
        attachments: list[ImageArtifactInput],
    ) -> tuple[str, list[dict[str, object]]]:
        descriptions: list[str] = []
        blocks: list[dict[str, object]] = []
        for attachment in attachments:
            try:
                data = await self._artifact_store.read_bytes(
                    attachment.sha256,
                    max_bytes=2 * 1024 * 1024,
                )
                metadata = inspect_image(data)
            except (ArtifactError, OSError, ValueError) as exc:
                raise HandlerError(INVALID_PARAMS, f"invalid image artifact: {exc}") from exc
            if (
                len(data) != attachment.size
                or metadata.media_type != attachment.media_type
                or metadata.width != attachment.width
                or metadata.height != attachment.height
            ):
                raise HandlerError(INVALID_PARAMS, "image artifact metadata mismatch")
            descriptions.append(
                "[attached image: "
                f"artifact:{attachment.sha256} {metadata.media_type} "
                f"{metadata.width}x{metadata.height} {len(data)} bytes]"
            )
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": metadata.media_type,
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                }
            )
        return "\n".join(descriptions), blocks

    # 首次异步操作前将文件 session 索引幂等导入 runtime
    async def _ensure_runtime_sessions(self) -> None:
        if self._runtime is None or self._runtime_bootstrapped:
            return
        async with self._runtime_bootstrap_lock:
            if self._runtime_bootstrapped:
                return
            sessions = list(self._sessions.values())
            turn_times: dict[str, tuple[str, str]] = {}
            for session in sessions:
                turn_times.update(self._store.run_time_ranges(session.id))
            await self._runtime.bootstrap_sessions(sessions, turn_times)
            self._runtime_bootstrapped = True
            await self._prune_stale_empty_sessions()
            if not self._queue_recovered:
                recovered = await self._runtime.recover_queued_messages(datetime.now(UTC))
                self._queue_recovered = True
                if recovered:
                    logger.warning(
                        "marked %d interrupted queued messages for confirmation",
                        recovered,
                    )
                for session_id in self._sessions:
                    self._schedule_queue_dispatch(session_id)

    # 删除超过保留期且从未使用的无标题会话，避免首启列表长期堆积
    async def _prune_stale_empty_sessions(self) -> None:
        cutoff = datetime.now(UTC) - _EMPTY_SESSION_RETENTION
        # 持久队列仍有消息（含 blocked）的会话是用户资产，绝不参与 prune
        queued_thread_ids = await self._queued_thread_ids_guard()
        if queued_thread_ids is None:
            return
        stale_ids: list[str] = []
        for session in tuple(self._sessions.values()):
            if (
                session.mode != "chat"
                or session.run_ids
                or session.title.strip() not in {"", "Untitled"}
                or self._locks[session.id].locked()
            ):
                continue
            if session.id in queued_thread_ids:
                continue
            try:
                updated_at = datetime.fromisoformat(
                    session.updated_at.replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if updated_at > cutoff or self._store.read_messages(session.id):
                continue
            stale_ids.append(session.id)
        for session_id in stale_ids:
            try:
                self._store.delete_session(session_id)
            except OSError:
                logger.warning(
                    "stale empty session cleanup failed sid=%s",
                    session_id,
                    exc_info=True,
                )
                continue
            self._sessions.pop(session_id, None)
            self._locks.pop(session_id, None)
            if self._runtime is not None:
                try:
                    await self._runtime.delete_session(session_id)
                except Exception:
                    logger.warning(
                        "stale empty runtime projection cleanup failed sid=%s",
                        session_id,
                        exc_info=True,
                    )
            await self._bus.publish(
                SessionDeletedEvent(session_id=session_id, ts=_now())
            )

    # 读取仍有持久消息的 thread；查询失败返回 None，使清理流程失败关闭
    async def _queued_thread_ids_guard(self) -> set[str] | None:
        if self._runtime is None:
            return set()
        try:
            return await self._runtime.thread_ids_with_queued_messages()
        except Exception:
            logger.warning(
                "queued thread lookup failed; stale session prune skipped",
                exc_info=True,
            )
            return None

    # 从磁盘恢复会话索引；active 表示 daemon 在一次 run 中退出，恢复为 interrupted
    def _rehydrate(self) -> None:
        for session in self._store.list_sessions():
            if not session.workspace:
                session.workspace = str(self._workspace)
                self._store.write_meta(session)
            if Path(session.workspace).resolve() != self._workspace:
                continue
            if session.status == "active":
                if session.run_ids:
                    if self._store.has_damaged_ledger(session.id):
                        self._store.recover_incomplete_tail(session.id)
                    session.status = "interrupted"
                else:
                    session.status = "waiting_for_input"
                self._store.write_meta(session)
            elif session.status == "interrupted" and not session.run_ids:
                session.status = "waiting_for_input"
                self._store.write_meta(session)
            if session.status == "interrupted":
                self._pending_recoveries.add(session.id)
            self._sessions[session.id] = session
            self._locks[session.id] = asyncio.Lock()

    # 创建新 session 并写入 meta.json
    async def create(
        self,
        mode: SessionMode,
        title: str = "",
        *,
        preset_id: str = "standard",
    ) -> Session:
        await self._ensure_runtime_sessions()
        preset = get_agent_preset(preset_id)
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        if self._hooks is not None:
            decision = await self._hooks.emit(
                "session_start",
                {"session_id": sid, "mode": mode, "title": title},
            )
            if decision.blocked:
                raise HandlerError(INVALID_PARAMS, decision.reason or "session blocked by hook")
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
            workspace=str(self._workspace),
            preset_id=preset.id,
            preset_digest=preset.digest,
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._pending_plans_loaded.add(sid)
        self._store.write_meta(session)
        if self._runtime is not None:
            await self._runtime.sync_session(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 从 durable runtime 事件恢复指定会话最终仍待处理的计划审批
    async def _load_pending_plan(self, sid: str) -> _PendingPlan | None:
        if sid in self._pending_plans_loaded:
            return self._pending_plans.get(sid)
        pending: _PendingPlan | None = None
        if self._runtime is not None:
            cursor = 0
            while True:
                events = await self._runtime.list_events(sid, after_seq=cursor, limit=1000)
                if not events:
                    break
                for event in events:
                    if event.type == "plan.ready" and event.turn_id:
                        pending = _PendingPlan(
                            session_id=sid,
                            run_id=event.turn_id,
                            request=str(event.payload.get("request", "")),
                            plan=str(event.payload.get("plan", "")),
                            plan_ticket=str(event.payload.get("plan_ticket", "")),
                        )
                    elif (
                        event.type == "plan.resolved"
                        and pending is not None
                        and event.turn_id == pending.run_id
                    ):
                        pending = None
                    elif (
                        event.type in {"turn.started", "run.started"}
                        and pending is not None
                        and event.turn_id != pending.run_id
                    ):
                        pending = None
                cursor = events[-1].seq
                if len(events) < 1000:
                    break
        if pending is None:
            self._pending_plans.pop(sid, None)
        else:
            self._pending_plans[sid] = pending
        self._pending_plans_loaded.add(sid)
        return pending

    # 校验并持久解决当前计划审批，拒绝过期 run 或重复决定
    async def respond_plan(
        self,
        sid: str,
        run_id: str,
        decision: Literal["approve", "revise", "cancel"],
        revision: str = "",
    ) -> PlanResolvedEvent:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        async with lock:
            pending = await self._load_pending_plan(sid)
            if pending is None:
                raise HandlerError(INVALID_PARAMS, "plan is not pending")
            if pending.run_id != run_id:
                raise HandlerError(INVALID_PARAMS, "plan response does not match the pending run")
            resolved = PlanResolvedEvent(
                session_id=sid,
                run_id=run_id,
                decision=decision,
                revision=revision.strip(),
                plan_ticket=pending.plan_ticket,
                ts=_now(),
            )
            await self._bus.publish(resolved)
            if self._runtime is not None:
                events = await self._runtime.list_turn_events(run_id)
                durable = any(
                    event.type == "plan.resolved"
                    and event.payload.get("decision") == decision
                    and str(event.payload.get("revision", "")) == revision.strip()
                    for event in events
                )
                if not durable:
                    raise HandlerError(
                        INVALID_PARAMS,
                        "plan response could not be persisted; retry after repairing audit storage",
                    )
            self._pending_plans.pop(sid, None)
            self._pending_plans_loaded.add(sid)
            return resolved

    # 从当前 Turn 的可信 Ledger 提取仍需用户批准的策略计划
    def _strategy_pending_plan(
        self,
        sid: str,
        run_id: str,
        request: str,
    ) -> _PendingPlan | None:
        strategy = ""
        latest_plan: dict[str, Any] | None = None
        for event in self._store.read_session_events(sid):
            if event.turn_id != run_id:
                continue
            if event.type == "task.profiled":
                profile = event.payload.get("profile")
                if isinstance(profile, dict):
                    strategy = str(profile.get("strategy", ""))
            elif event.type == "plan.updated":
                latest_plan = event.payload
        if strategy != "plan_first" or latest_plan is None:
            return None
        raw_steps = latest_plan.get("plan")
        if not isinstance(raw_steps, list) or not raw_steps:
            return None
        lines: list[str] = []
        explanation = str(latest_plan.get("explanation", "")).strip()
        if explanation:
            lines.append(explanation)
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                continue
            step = str(raw_step.get("step", "")).strip()
            if step:
                lines.append(f"{index}. {step}")
        if not lines:
            return None
        return _PendingPlan(
            session_id=sid,
            run_id=run_id,
            request=request,
            plan="\n".join(lines),
            plan_ticket=str(latest_plan.get("plan_ticket", "")),
        )

    # 返回供 Change Center 与 rewind 使用的独占工作区变更上下文
    def workspace_mutation(self) -> Any:
        return self._workspace_mutation_guard.mutation()

    # 持久化一条跨 TUI/Web 共享的后续消息并启动串行派发
    async def queue_message(
        self,
        sid: str,
        content: str,
        *,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        attachments: list[ImageArtifactInput] | None = None,
        display_content: str | None = None,
    ) -> QueuedMessageRecord:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        if session.status == "closed":
            raise HandlerError(SESSION_CLOSED, "session already closed")
        if self._runtime is None:
            raise HandlerError(INVALID_PARAMS, "durable message queue is unavailable")
        visible = (display_content or content).strip()
        if not content.strip() or not visible:
            raise HandlerError(INVALID_PARAMS, "queued message must not be blank")
        now = datetime.now(UTC)
        record = QueuedMessageRecord(
            id=f"queue-{uuid.uuid4().hex}",
            thread_id=sid,
            content=content,
            display_content=visible,
            mode=runtime_mode,
            attachments=attachments or [],
            created_at=now,
            updated_at=now,
        )
        await self._runtime.enqueue_message(record)
        self._schedule_queue_dispatch(sid)
        return record

    # 返回指定会话的持久消息队列
    async def list_queued_messages(self, sid: str) -> list[QueuedMessageRecord]:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        if self._runtime is None:
            return []
        return await self._runtime.list_queued_messages(sid)

    # 删除一条尚未完成的排队消息
    async def remove_queued_message(self, sid: str, message_id: str) -> None:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        if self._runtime is None:
            raise HandlerError(INVALID_PARAMS, "durable message queue is unavailable")
        try:
            await self._runtime.remove_queued_message(
                sid,
                message_id,
                reason="cancelled_by_user",
                ts=datetime.now(UTC),
            )
        except QueuedMessageDispatchingError as exc:
            raise HandlerError(
                INVALID_PARAMS,
                "message is already running; stop the active turn instead",
            ) from exc

    # 将中断或启动失败的 blocked 消息重新排入队列
    async def retry_queued_message(self, sid: str, message_id: str) -> None:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        if self._runtime is None:
            raise HandlerError(INVALID_PARAMS, "durable message queue is unavailable")
        await self._runtime.retry_queued_message(
            sid,
            message_id,
            datetime.now(UTC),
        )
        self._schedule_queue_dispatch(sid)

    # 为指定会话创建唯一队列消费任务并记录唤醒信号
    def _schedule_queue_dispatch(self, sid: str) -> None:
        if self._queue_closing or self._runtime is None or sid not in self._sessions:
            return
        wakeup = self._queue_wakeups.setdefault(sid, asyncio.Event())
        wakeup.set()
        existing = self._queue_dispatch_tasks.get(sid)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._dispatch_queued_messages(sid),
            name=f"message-queue:{sid}",
        )
        self._queue_dispatch_tasks[sid] = task

        # 回收消费任务，并在结束竞态中仍有新消息时重新启动
        def cleanup(completed: asyncio.Task[None]) -> None:
            current = self._queue_dispatch_tasks.get(sid)
            if current is completed:
                self._queue_dispatch_tasks.pop(sid, None)
            if not completed.cancelled():
                error = completed.exception()
                if error is not None:
                    logger.error(
                        "message queue dispatch failed sid=%s error_type=%s",
                        sid,
                        type(error).__name__,
                    )
            pending_wakeup = self._queue_wakeups.get(sid)
            if pending_wakeup is not None and pending_wakeup.is_set():
                self._schedule_queue_dispatch(sid)

        task.add_done_callback(cleanup)

    # 串行领取并执行消息，启动失败时保留为用户可见的 blocked 状态
    async def _dispatch_queued_messages(self, sid: str) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        wakeup = self._queue_wakeups.setdefault(sid, asyncio.Event())
        while sid in self._sessions:
            wakeup.clear()
            while self._locks[sid].locked():
                await asyncio.sleep(0.05)
            record = await runtime.claim_next_queued_message(sid, datetime.now(UTC))
            if record is None:
                if wakeup.is_set():
                    continue
                return
            # 每次派发铸全新 run_id：崩溃恢复后 retry 不能复用旧 id，否则
            # create_turn 主键冲突使该消息永久不可重试，且 run 目录可安全做路径组件
            dispatch_run_id = f"{record.id}-{uuid.uuid4().hex[:8]}"
            try:
                await self.send_message(
                    sid,
                    record.content,
                    run_id=dispatch_run_id,
                    runtime_mode=record.mode,
                    attachments=record.attachments,
                    display_content=record.display_content,
                )
            except HandlerError as exc:
                if exc.code == SESSION_BUSY:
                    await runtime.defer_queued_message(
                        record,
                        datetime.now(UTC),
                    )
                    wakeup.set()
                    continue
                else:
                    await runtime.block_queued_message(
                        record,
                        str(exc),
                        datetime.now(UTC),
                    )
                return
            except Exception as exc:
                logger.warning(
                    "queued message could not start sid=%s queue_id=%s",
                    sid,
                    record.id,
                    exc_info=True,
                )
                await runtime.block_queued_message(
                    record,
                    f"{type(exc).__name__}: {exc}",
                    datetime.now(UTC),
                )
                return
            await runtime.remove_queued_message(
                sid,
                record.id,
                reason="dispatched",
                ts=datetime.now(UTC),
            )
            wakeup.set()

    # 在工作区共享 Turn 门闩内处理用户消息
    async def send_message(
        self,
        sid: str,
        content: str,
        *,
        run_id: str | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        attachments: list[ImageArtifactInput] | None = None,
        display_content: str | None = None,
    ) -> str:
        async with self._workspace_mutation_guard.turn():
            return await self._send_message(
                sid,
                content,
                run_id=run_id,
                runtime_mode=runtime_mode,
                attachments=attachments,
                display_content=display_content,
            )

    # 追加 thread 并启动一次 agent run
    async def _send_message(
        self,
        sid: str,
        content: str,
        *,
        run_id: str | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        attachments: list[ImageArtifactInput] | None = None,
        display_content: str | None = None,
    ) -> str:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")
            self._refresh_stable_preset(session)
            pending_plan = await self._load_pending_plan(sid)
            if pending_plan is not None:
                raise HandlerError(
                    INVALID_PARAMS,
                    "pending plan must be approved, revised, or cancelled before a new turn",
                )

            active_goal_candidate: GoalRecord | None = None
            active_goal_authority: AuthoritySnapshot | None = None
            if self._goal_service is not None:
                candidate = self._goal_service.current(sid)
                if candidate is not None and candidate.status == "active":
                    active_goal_candidate = candidate
                    active_goal_authority = (
                        self._authority_provider(sid)
                        if self._authority_provider is not None
                        else None
                    )
                    if candidate.auto_continue and candidate.linked_run_ids:
                        decision = self._goal_service.decide_continue(
                            candidate.id,
                            current_authority=active_goal_authority,
                        )
                        if not decision.should_continue:
                            raise HandlerError(
                                INVALID_PARAMS,
                                "goal continuation requires user confirmation: "
                                f"{decision.reason}",
                            )

            resolved_route: ResolvedRoute | None = None
            if self._route_registry is not None:
                try:
                    resolved_route = self._route_registry.resolve()
                except RouteResolutionError as exc:
                    raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            image_attachments = attachments or []
            attachment_text, image_blocks = await self._prepare_image_attachments(
                image_attachments
            )
            ledger_content = (
                f"{content.rstrip()}\n\n{attachment_text}".strip()
                if attachment_text
                else content
            )

            run_id = run_id or new_run_id()
            requested_skill: Skill | None = None
            skill_name = ""
            skill_arguments = ""
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                skill_arguments = parts[1] if len(parts) > 1 else ""
                try:
                    workspace_trusted = (
                        self._authority_provider is not None
                        and self._authority_provider(sid).workspace_trust
                        == WorkspaceTrust.TRUSTED
                    )
                    requested_skill = self._skill_loader.resolve(
                        skill_name,
                        require_trusted=True,
                        workspace_trusted=workspace_trusted,
                    )
                except SkillError as exc:
                    raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            if self._hooks is not None:
                message_decision = await self._hooks.emit(
                    "message_submit",
                    {"session_id": sid, "run_id": run_id, "content": ledger_content},
                )
                if message_decision.blocked:
                    raise HandlerError(
                        INVALID_PARAMS,
                        message_decision.reason or "message blocked by hook",
                    )
                turn_decision = await self._hooks.emit(
                    "turn_start",
                    {
                        "session_id": sid,
                        "run_id": run_id,
                        "runtime_mode": runtime_mode.value,
                    },
                )
                if turn_decision.blocked:
                    raise HandlerError(
                        INVALID_PARAMS,
                        turn_decision.reason or "turn blocked by hook",
                    )
            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = ledger_content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if requested_skill is not None:
                workspace_trusted = (
                    self._authority_provider is not None
                    and self._authority_provider(sid).workspace_trust
                    == WorkspaceTrust.TRUSTED
                )
                goal = self._skill_loader.render_prompt(
                    requested_skill,
                    skill_arguments,
                    require_trusted=True,
                    workspace_trusted=workspace_trusted,
                )
                system_prompt_override = requested_skill.system_prompt_template
                tool_whitelist = requested_skill.allowed_tools or None

            runner = self._runner_factory()
            if resolved_route is not None:
                resolved_route = await runner.resolve_turn_binding(
                    resolved_route=resolved_route,
                    runtime_mode=runtime_mode,
                    run_id=run_id,
                )
            if (
                image_attachments
                and resolved_route is not None
                and not resolved_route.route.supports_images
            ):
                raise HandlerError(
                    INVALID_PARAMS,
                    "selected Turn route does not support images; "
                    "select an image-capable route",
                )
            active_goal: GoalRecord | None = None
            continuation_decision: GoalContinueDecision | None = None
            persistent_goal_context = ""
            goal_wall_timeout_s: float | None = None
            if self._goal_service is not None and active_goal_candidate is not None:
                active_goal = self._goal_service.start_run(
                    active_goal_candidate.id,
                    run_id,
                    current_authority=active_goal_authority,
                )
                persistent_goal_context = self._goal_service.render_context(active_goal)
                goal_wall_timeout_s = self._goal_service.remaining_wall_seconds(
                    active_goal.id
                )
            run_options: dict[str, Any] = {
                "run_id": run_id,
                "session": session,
                "store": self._store,
                "system_prompt_override": system_prompt_override,
                "tool_whitelist": tool_whitelist,
                "runtime_mode": runtime_mode,
            }
            if persistent_goal_context:
                run_options["persistent_goal_context"] = persistent_goal_context
            if resolved_route is not None:
                run_options["resolved_route"] = resolved_route
                run_options["resolved_route_is_explicit"] = True
            if image_blocks:
                run_options["initial_images"] = image_blocks

            persistence_ready = asyncio.Event()

            # 让可取消 runner 在所有 session/runtime 写入完成前停在内存屏障
            async def execute_after_persistence() -> Any:
                await persistence_ready.wait()
                if goal_wall_timeout_s is None:
                    return await runner.run_and_capture(goal, **run_options)
                from code_rook.core.runner import RunOutcome

                try:
                    async with asyncio.timeout(goal_wall_timeout_s):
                        return await runner.run_and_capture(goal, **run_options)
                except TimeoutError:
                    return RunOutcome(
                        status="failed",
                        result="",
                        reason="max_wall_seconds_reached",
                    )

            runner_task = asyncio.create_task(
                execute_after_persistence(),
                name=f"run:{run_id}",
            )
            active = _ActiveRun(
                session_id=sid,
                task=runner_task,
                finished=asyncio.Event(),
            )
            self._active_runs[run_id] = active
            runtime_started = False
            runtime_attempted = False
            try:
                resumed_from_interruption = (
                    session.status == "interrupted" or sid in self._pending_recoveries
                )
                if session.status in ("waiting_for_input", "interrupted"):
                    await self._bus.publish(
                        SessionResumedEvent(session_id=sid, ts=_now())
                    )
                if resumed_from_interruption:
                    await self._bus.publish(
                        RecoveryResolvedEvent(
                            session_id=sid,
                            run_id=session.run_ids[-1] if session.run_ids else "",
                            action="continue_with_new_turn",
                            ts=_now(),
                        )
                    )
                    self._pending_recoveries.discard(sid)
                self._store.append_message(
                    sid,
                    "user",
                    ledger_content,
                    run_id=run_id,
                    message_id=f"{run_id}:user",
                )
                await self._bus.publish(
                    SessionMessageReceivedEvent(
                        session_id=sid,
                        content=ledger_content,
                        ts=_now(),
                    )
                )

                if not session.title:
                    session.title = (display_content or content)[:40]
                if run_id not in session.run_ids:
                    session.run_ids.append(run_id)
                session.status = "active"
                session.updated_at = _now()
                self._store.write_meta(session)
                if self._runtime is not None:
                    runtime_attempted = True
                    await self._runtime.start_turn(
                        session,
                        run_id,
                        ledger_content,
                        runtime_mode=runtime_mode,
                        display_content=display_content,
                        route=(
                            resolved_route.receipt
                            if resolved_route is not None
                            else None
                        ),
                    )
                    runtime_started = True
                if requested_skill is not None:
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=skill_arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )
                persistence_ready.set()
                outcome = await runner_task
            except asyncio.CancelledError:
                if not runner_task.done():
                    runner_task.cancel()
                    await asyncio.gather(runner_task, return_exceptions=True)
                if active_goal is not None and self._goal_service is not None:
                    await self._finish_goal_run(
                        active_goal.id,
                        run_id,
                        succeeded=False,
                        reason="cancelled",
                    )
                session.status = "interrupted"
                session.updated_at = _now()
                self._store.write_meta(session)
                if self._runtime is not None and runtime_started:
                    await self._runtime.finish_turn(
                        session,
                        run_id,
                        TurnStatus.INTERRUPTED,
                        reason="cancelled",
                    )
                await self._bus.publish(
                    SessionInterruptedEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        reason="cancelled",
                        ts=session.updated_at,
                    )
                )
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return run_id
            except Exception as exc:
                if not persistence_ready.is_set() and not runner_task.done():
                    runner_task.cancel()
                    await asyncio.gather(runner_task, return_exceptions=True)
                if active_goal is not None and self._goal_service is not None:
                    try:
                        latest = self._goal_service.get(active_goal.id)
                        if latest.current_run_id == run_id:
                            self._goal_service.abort_run(
                                active_goal.id,
                                run_id,
                                reason=(
                                    f"runner failed: {type(exc).__name__}"
                                    if persistence_ready.is_set()
                                    else "turn preparation failed: "
                                    f"{type(exc).__name__}"
                                ),
                            )
                    except (OSError, ValueError):
                        logger.exception(
                            "could not release failed goal run reservation run_id=%s",
                            run_id,
                        )
                session.status = "interrupted"
                session.updated_at = _now()
                try:
                    self._store.write_meta(session)
                except OSError:
                    logger.exception(
                        "could not persist interrupted session after turn failure run_id=%s",
                        run_id,
                    )
                if self._runtime is not None and runtime_attempted:
                    try:
                        await self._runtime.finish_turn(
                            session,
                            run_id,
                            TurnStatus.FAILED,
                            reason=(
                                "runner_failed"
                                if persistence_ready.is_set()
                                else "turn_preparation_failed"
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "could not finalize failed runtime turn run_id=%s",
                            run_id,
                        )
                raise
            finally:
                self._active_runs.pop(run_id, None)
                active.finished.set()

            session.updated_at = _now()
            if active_goal is not None and self._goal_service is not None:
                continuation_decision = await self._finish_goal_run(
                    active_goal.id,
                    run_id,
                    succeeded=outcome.status == "success",
                    reason=outcome.reason or "",
                )
            if (
                runtime_mode == RuntimeMode.PLAN
                and outcome.status == "success"
                and outcome.result.strip()
            ):
                self._pending_plans[sid] = _PendingPlan(
                    session_id=sid,
                    run_id=run_id,
                    request=content,
                    plan=outcome.result.strip(),
                )
                self._pending_plans_loaded.add(sid)
                await self._bus.publish(
                    PlanReadyEvent(
                        session_id=sid,
                        run_id=run_id,
                        request=content,
                        plan=outcome.result.strip(),
                        ts=session.updated_at,
                    )
                )
            elif outcome.status == "success":
                strategy_plan = self._strategy_pending_plan(sid, run_id, content)
                if strategy_plan is not None:
                    self._pending_plans[sid] = strategy_plan
                    self._pending_plans_loaded.add(sid)
                    await self._bus.publish(
                        PlanReadyEvent(
                            session_id=sid,
                            run_id=run_id,
                            request=content,
                            plan=strategy_plan.plan,
                            plan_ticket=strategy_plan.plan_ticket,
                            ts=session.updated_at,
                        )
                    )
            if session.mode == "one_shot":
                if self._hooks is not None:
                    await self._hooks.emit(
                        "session_stop",
                        {"session_id": sid, "run_id": run_id, "reason": "one_shot_complete"},
                    )
                session.status = "closed"
                await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            if self._runtime is not None:
                runtime_status = (
                    TurnStatus.COMPLETED
                    if outcome.status == "success"
                    else TurnStatus.FAILED
                )
                await self._runtime.finish_turn(
                    session,
                    run_id,
                    runtime_status,
                    reason=outcome.reason,
                    result=outcome.result,
                )
            if (
                active_goal is not None
                and continuation_decision is not None
                and (
                    continuation_decision.should_continue
                    or continuation_decision.reason == "token_budget_reserved"
                )
                and session.mode == "chat"
            ):
                retry_delay_s = (
                    min(30.0, float(2 ** continuation_decision.auto_turns_used))
                    if outcome.status != "success"
                    else 0.0
                )
                self._schedule_goal_continuation(
                    active_goal.id,
                    session.id,
                    delay_s=retry_delay_s,
                )
            return run_id

    # 返回指定会话当前是否正持有 turn 执行锁
    def is_busy(self, sid: str) -> bool:
        self._get_session(sid)
        return self._locks[sid].locked() or any(
            continuation.session_id == sid and not continuation.task.done()
            for continuation in self._goal_continuations.values()
        )

    # 返回指定 session 的 active run ID，不存在时返回 None
    def active_run_id(self, sid: str) -> str | None:
        self._get_session(sid)
        for run_id, active in self._active_runs.items():
            if active.session_id == sid and not active.task.done():
                return run_id
        return None

    # 返回当前 workspace 正在执行的 run 数，供启动器安全切换 daemon 工作目录
    def active_run_count(self) -> int:
        sessions = {
            active.session_id
            for active in self._active_runs.values()
            if not active.task.done()
        }
        sessions.update(
            continuation.session_id
            for continuation in self._goal_continuations.values()
            if not continuation.task.done()
        )
        return len(sessions)

    async def cancel_run(self, run_id: str) -> str:
        active = self._active_runs.get(run_id)
        if active is None or active.task.done():
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        if not active.task.cancel():
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        try:
            await active.task
        except asyncio.CancelledError:
            pass
        if self._subagent_registry is not None:
            await self._subagent_registry.cancel_descendants(run_id)
        await active.finished.wait()
        return active.session_id

    # 将用户新指令排入活动 run，在下一次模型决策前注入
    async def steer_run(self, run_id: str, content: str) -> str:
        active = self._active_runs.get(run_id)
        if (
            active is None
            or active.task.done()
            or self._interaction_manager is None
            or not self._interaction_manager.steer(run_id, content)
        ):
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        await self._bus.publish(
            RunSteeredEvent(
                run_id=run_id,
                session_id=active.session_id,
                content=content.strip(),
                ts=_now(),
            )
        )
        return active.session_id

    async def cancel_all(self) -> None:
        self._queue_closing = True
        for wakeup in self._queue_wakeups.values():
            wakeup.clear()
        queue_tasks = [
            task for task in self._queue_dispatch_tasks.values() if not task.done()
        ]
        for task in queue_tasks:
            task.cancel()
        if queue_tasks:
            await asyncio.gather(*queue_tasks, return_exceptions=True)
        continuation_tasks = [
            continuation.task
            for continuation in self._goal_continuations.values()
            if not continuation.task.done()
        ]
        for task in continuation_tasks:
            task.cancel()
        if continuation_tasks:
            await asyncio.gather(*continuation_tasks, return_exceptions=True)
        run_ids = list(self._active_runs)
        if not run_ids:
            return
        await asyncio.gather(
            *(self.cancel_run(run_id) for run_id in run_ids),
            return_exceptions=True,
        )

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            if self._hooks is not None:
                await self._hooks.emit(
                    "session_stop",
                    {"session_id": sid, "reason": "closed"},
                )
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        provider = self._provider
        if self._route_registry is not None:
            from code_rook.core.llm.factory import create_provider_for_route

            try:
                resolved_route = self._route_registry.resolve()
            except RouteResolutionError as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            provider = create_provider_for_route(
                resolved_route.route,
                resolved_route.credential,
            )
        if provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from code_rook.core.bus.commands import SessionCompactResult
            from code_rook.core.compact.compactor import Compactor
            messages = self._store.read_messages(sid)
            session_dir = self._store.session_dir(sid)
            compactor = Compactor(self._bus, session_dir, sid, store=self._store)
            result = await compactor.compact_messages(messages, provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            await compactor.commit(
                result,
                run_id="manual",
                trigger="manual",
                publish=False,
            )
            if self._hooks is not None:
                await self._hooks.emit(
                    "compaction_completed",
                    {
                        "session_id": sid,
                        "run_id": "manual",
                        "trigger": "manual",
                        "summary_path": result.summary_path,
                        "saved_tokens": max(
                            0,
                            result.original_token_estimate - result.compacted_tokens,
                        ),
                    },
                )
            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(0, result.original_token_estimate - result.compacted_tokens),
                original_tokens=result.original_token_estimate,
                compacted_tokens=result.compacted_tokens,
                retained_tokens=result.retained_tokens,
                retained_messages=result.retained_messages,
                quality_score=result.quality.score,
                summary_path=result.summary_path,
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 返回最近一次 run 的任务列表，未创建任务时保持只读且返回空集合
    def list_tasks(self, sid: str) -> tuple[str | None, list[dict[str, Any]]]:
        session = self._get_session(sid)
        run_id = session.run_ids[-1] if session.run_ids else None
        if run_id is None:
            return None, []
        tasks_dir = self._store.runs_dir(sid) / run_id / ".tasks"
        if not tasks_dir.is_dir():
            return run_id, []
        tasks: list[Task] = []
        for path in tasks_dir.glob("task_*.json"):
            try:
                tasks.append(Task.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        tasks.sort(key=lambda task: task.id)
        return run_id, [task.to_dict() for task in tasks]

    # 返回指定（默认最近一次）run 的 checkpoint 元数据，不创建任何缺失目录
    def list_checkpoints(
        self, sid: str, run_id: str | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        session = self._get_session(sid)
        run_id = self._resolve_run_id(session, run_id)
        if run_id is None:
            return None, []
        root = self._store.runs_dir(sid) / run_id / ".checkpoints"
        if not root.is_dir():
            return run_id, []
        store = CheckpointStore(root, WorkspaceBoundary.current(), create=False)
        checkpoints = [
            {
                "checkpoint_id": item.checkpoint_id,
                "label": item.label,
                "created_at": item.created_at,
                "status": item.status,
                "paths": item.paths,
            }
            for item in store.list_checkpoints()
        ]
        return run_id, checkpoints

    # 读取指定 checkpoint 的恢复范围、冲突和当前状态摘要，不修改任何文件
    def preview_rewind(
        self,
        sid: str,
        checkpoint_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(sid)
        resolved_run_id = self._resolve_run_id(session, run_id)
        if resolved_run_id is None:
            raise HandlerError(INVALID_PARAMS, "session has no run checkpoints")
        root = self._store.runs_dir(sid) / resolved_run_id / ".checkpoints"
        try:
            preview = CheckpointStore(
                root,
                WorkspaceBoundary.current(),
                create=False,
            ).preview_rewind(checkpoint_id)
        except CheckpointError as exc:
            raise HandlerError(
                INVALID_PARAMS,
                str(exc),
                {"code": exc.code, "conflicts": exc.conflicts},
            ) from exc
        return {
            "checkpoint_id": preview.checkpoint_id,
            "paths": preview.paths,
            "restorable": preview.restorable,
            "already_restored": preview.already_restored,
            "conflicts": preview.conflicts,
            "state_digest": preview.state_digest,
        }

    # 安全恢复指定（默认最近一次）run 中用户明确选择的 checkpoint
    def rewind(
        self,
        sid: str,
        checkpoint_id: str,
        run_id: str | None = None,
        *,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        session = self._get_session(sid)
        if self._locks[sid].locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        run_id = self._resolve_run_id(session, run_id)
        if run_id is None:
            raise HandlerError(INVALID_PARAMS, "session has no run checkpoints")
        root = self._store.runs_dir(sid) / run_id / ".checkpoints"
        try:
            outcome = CheckpointStore(root, WorkspaceBoundary.current()).rewind(
                checkpoint_id,
                expected_digest=expected_digest,
            )
        except CheckpointError as exc:
            raise HandlerError(
                INVALID_PARAMS,
                str(exc),
                {"code": exc.code, "conflicts": exc.conflicts},
            ) from exc
        return {
            "checkpoint_id": outcome.checkpoint_id,
            "restored": outcome.restored,
            "already_restored": outcome.already_restored,
        }

    # 解析目标 run_id：为空取最近一次 run，非空则校验属于该 session
    def _resolve_run_id(self, session: Session, run_id: str | None) -> str | None:
        if run_id is None:
            return session.run_ids[-1] if session.run_ids else None
        if run_id not in session.run_ids:
            raise HandlerError(INVALID_PARAMS, f"unknown run_id for session: {run_id}")
        return run_id

    # 返回当前 transcript 的消息数、确定性 token 估算和 run 概览
    def context_info(self, sid: str) -> dict[str, Any]:
        session = self._get_session(sid)
        messages = self._store.read_messages(sid)
        return {
            "message_count": len(messages),
            "estimated_tokens": estimate_messages_tokens(messages),
            "run_count": len(session.run_ids),
            "last_run_id": session.run_ids[-1] if session.run_ids else None,
            "memory_count": len(
                MemoryStore(WorkspaceBoundary.current().root / ".coderook" / "memory").list_all()
            ),
        }

    # 返回已恢复的指定会话，供 Core 的会话级配置命令做存在性校验
    def get_session(self, sid: str) -> Session:
        return self._get_session(sid)

    # 返回最近更新的 session 元数据，供 CLI/TUI 选择历史会话
    async def list_sessions(
        self,
        *,
        include_closed: bool = False,
        limit: int = 50,
    ) -> list[Session]:
        await self._ensure_runtime_sessions()
        sessions = sorted(
            self._sessions.values(),
            key=lambda session: session.updated_at,
            reverse=True,
        )
        if not include_closed:
            sessions = [session for session in sessions if session.status != "closed"]
        return sessions[:limit]

    # 重新打开一个持久化 chat session，使后续消息沿用原 thread
    async def resume(self, sid: str) -> Session:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if session.mode != "chat":
            raise HandlerError(SESSION_NOT_RESUMABLE, "only chat sessions can be resumed")

        async with lock:
            was_interrupted = session.status == "interrupted"
            interrupted_run_id = session.run_ids[-1] if session.run_ids else ""
            session.status = "waiting_for_input"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=session.updated_at))
            if was_interrupted:
                self._pending_recoveries.add(sid)
                pending = self._store.find_incomplete_tool_calls(sid)
                read_only_tools = {
                    "artifact_read",
                    "glob",
                    "grep",
                    "list_dir",
                    "read_file",
                    "read_image",
                    "repository",
                    "tool_search",
                }
                safe_to_resume = not pending or all(
                    call.tool_name in read_only_tools for call in pending
                )
                interruption_kind = "turn_interrupted"
                summary = "会话已恢复，可从最后一个持久化步骤继续。"
                if pending and safe_to_resume:
                    interruption_kind = "read_tool_interrupted"
                    summary = "只读工具在中断时未返回，可安全重新读取后继续。"
                elif pending:
                    interruption_kind = "tool_state_unknown"
                    summary = "修改或命令状态不确定，请先查看中断前变更。"
                await self._bus.publish(
                    RecoveryAvailableEvent(
                        session_id=sid,
                        run_id=interrupted_run_id,
                        interruption_kind=interruption_kind,
                        safe_to_resume=safe_to_resume,
                        summary=summary,
                        actions=[
                            "continue",
                            "view_changes",
                            "rewind_checkpoint",
                            "abandon_turn",
                            "export_diagnostics",
                        ],
                        ts=session.updated_at,
                    )
                )
        return session

    async def rename(self, sid: str, title: str) -> Session:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        normalized = title.strip()
        if not normalized:
            raise HandlerError(INVALID_PARAMS, "session title must not be blank")
        async with lock:
            session.title = normalized
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(
                SessionRenamedEvent(
                    session_id=sid,
                    title=normalized,
                    ts=session.updated_at,
                )
            )
        return session

    # 从现有会话创建历史副本，并允许仅在新 fork 上冻结不同 Preset
    async def fork(
        self,
        sid: str,
        title: str = "",
        *,
        preset_id: str | None = None,
    ) -> Session:
        await self._ensure_runtime_sessions()
        source = self._get_session(sid)
        source_lock = self._locks[sid]
        if source_lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with source_lock:
            fork_id = f"sess-{uuid.uuid4().hex[:12]}"
            ts = _now()
            fork_title = title.strip() or f"{source.title or source.id} (fork)"
            preset = get_agent_preset(preset_id or source.preset_id)
            forked = Session(
                id=fork_id,
                mode="chat",
                status="waiting_for_input",
                title=fork_title[:200],
                created_at=ts,
                updated_at=ts,
                run_ids=[],
                parent_session_id=source.id,
                workspace=source.workspace,
                preset_id=preset.id,
                preset_digest=preset.digest,
            )
            self._store.create_fork(source.id, forked)
            self._sessions[fork_id] = forked
            self._locks[fork_id] = asyncio.Lock()
            self._pending_plans_loaded.add(fork_id)
            if self._runtime is not None:
                await self._runtime.sync_session(forked)
            await self._bus.publish(
                SessionCreatedEvent(session_id=fork_id, mode="chat", ts=ts)
            )
            await self._bus.publish(
                SessionForkedEvent(
                    session_id=fork_id,
                    source_session_id=source.id,
                    ts=ts,
                )
            )
        return forked

    async def export(
        self,
        sid: str,
        export_format: SessionExportFormat,
    ) -> tuple[str, str, str]:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            return export_session(
                session,
                self._store.read_messages(sid),
                self._store.read_notes(sid),
                export_format,
            )

    async def delete(self, sid: str) -> None:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            if self._hooks is not None:
                await self._hooks.emit(
                    "session_stop",
                    {"session_id": sid, "reason": "deleted"},
                )
            self._store.delete_session(sid)
        queue_task = self._queue_dispatch_tasks.pop(sid, None)
        self._queue_wakeups.pop(sid, None)
        if queue_task is not None and not queue_task.done():
            queue_task.cancel()
            await asyncio.gather(queue_task, return_exceptions=True)
        self._sessions.pop(sid, None)
        self._locks.pop(sid, None)
        self._pending_plans.pop(sid, None)
        self._pending_plans_loaded.discard(sid)
        if self._runtime is not None:
            await self._runtime.delete_session(sid)
        await self._bus.publish(SessionDeletedEvent(session_id=sid, ts=_now()))

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            try:
                session = self._store.read_meta(sid)
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                raise HandlerError(SESSION_NOT_FOUND, "session not found") from None
            if not session.workspace:
                session.workspace = str(self._workspace)
                self._store.write_meta(session)
            if Path(session.workspace).resolve() != self._workspace:
                raise HandlerError(
                    SESSION_NOT_FOUND,
                    "session belongs to another workspace",
                )
            if session.status == "active":
                session.status = "interrupted"
                self._store.write_meta(session)
            self._sessions[sid] = session
            self._locks[sid] = asyncio.Lock()
        return session

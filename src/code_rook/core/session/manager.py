from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from code_rook.core.agent_runtime.images import normalize_tool_images
from code_rook.core.agent_runtime.prompt_templates import (
    expand_prompt_template,
    list_prompt_templates,
)
from code_rook.core.agent_runtime.skill_input import expand_skill_input
from code_rook.core.agent_runtime.user_shell import parse_user_shell
from code_rook.core.artifacts import (
    ArtifactError,
    ArtifactStore,
    ImageArtifactInput,
    inspect_image,
)
from code_rook.core.authority import AuthoritySnapshot, RuntimeMode, WorkspaceTrust
from code_rook.core.bus.envelope import INVALID_PARAMS, HandlerError
from code_rook.core.bus.events import (
    AgentMessageEvent,
    ExtensionNotificationEvent,
    ExtensionUiKind,
    ExtensionUiUpdatedEvent,
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
from code_rook.core.config import CompactionConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import FollowUpMessage, InteractionManager, UserMessageContent
from code_rook.core.llm.retry import RetryPolicy
from code_rook.core.llm.route_registry import RouteResolutionError
from code_rook.core.llm.routes import ThinkingLevel
from code_rook.core.memory import MemoryStore
from code_rook.core.presets import get_agent_preset
from code_rook.core.runs import new_run_id
from code_rook.core.runtime.models import QueuedMessageRecord, TurnStatus
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import QueuedMessageDispatchingError
from code_rook.core.session.exporter import SessionExportFormat, export_session
from code_rook.core.session.importer import SessionImportFormat, import_session_content
from code_rook.core.session.model import Session, SessionMode
from code_rook.core.session.store import SessionStore
from code_rook.core.skills.loader import SkillError, SkillLoader
from code_rook.core.task.model import Task
from code_rook.core.tools.base import ToolResult
from code_rook.core.workspace import WorkspaceBoundary

if TYPE_CHECKING:
    from code_rook.core.agent_runtime.extensions import ExtensionHost
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
_MAX_RESERVED_GOAL_WAITS = 50
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
        next_turn_workspace_edit_approver: Callable[[str], None] | None = None,
        workspace_mutation_guard: WorkspaceMutationGuard | None = None,
        workspace_mutation_lock: asyncio.Lock | None = None,
        workspace: Path | None = None,
        compaction_config: CompactionConfig | None = None,
        summary_retry_policy: RetryPolicy | None = None,
        prompt_paths: tuple[Path, ...] = (),
        skill_paths: tuple[Path, ...] = (),
        follow_up_mode: Literal["one-at-a-time", "all"] = "one-at-a-time",
        image_auto_resize: bool = True,
        shutdown_requester: Callable[[], Any] | None = None,
    ) -> None:
        if workspace_mutation_guard is not None and workspace_mutation_lock is not None:
            raise ValueError(
                "workspace_mutation_guard and workspace_mutation_lock are mutually exclusive"
            )
        self._store = store
        self._image_auto_resize = image_auto_resize
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._compaction_config = compaction_config or CompactionConfig()
        self._summary_retry_policy = summary_retry_policy
        self._prompt_paths = prompt_paths
        self._skill_paths = skill_paths
        self._follow_up_mode = follow_up_mode
        self._shutdown_requester = shutdown_requester
        self._subagent_registry = subagent_registry
        self._runtime = runtime_service
        self._interaction_manager = interaction_manager
        self._route_registry = route_registry
        self._hooks = hooks
        self._goal_service = goal_service
        self._authority_provider = authority_provider
        self._next_turn_workspace_edit_approver = next_turn_workspace_edit_approver
        self._workspace_mutation_guard = workspace_mutation_guard or WorkspaceMutationGuard(
            workspace_mutation_lock
        )
        self._runtime_bootstrapped = False
        self._runtime_bootstrap_lock = asyncio.Lock()
        self._turn_reservation_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._extension_hosts: dict[str, ExtensionHost] = {}
        self._extension_skill_loaders: dict[str, SkillLoader] = {}
        self._extension_prompt_paths: dict[str, tuple[Path, ...]] = {}
        self._extension_theme_paths: dict[str, tuple[Path, ...]] = {}
        self._extension_ui: dict[str, dict[str, Any]] = {}
        self._extension_resources_ready: set[str] = set()
        self._pending_extension_messages: dict[str, list[dict[str, Any]]] = {}
        self._next_turn_extension_messages: dict[str, list[dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._turn_reservations: dict[str, str] = {}
        self._active_runs: dict[str, _ActiveRun] = {}
        self._goal_continuations: dict[str, _GoalContinuation] = {}
        self._queue_dispatch_tasks: dict[str, asyncio.Task[None]] = {}
        self._queue_wakeups: dict[str, asyncio.Event] = {}
        self._queue_recovered = False
        self._queue_closing = False
        self._queue_paused: set[str] = set()
        self._pending_plans: dict[str, _PendingPlan] = {}
        self._pending_plans_loaded: set[str] = set()
        self._pending_recoveries: set[str] = set()
        workspace_root = workspace or WorkspaceBoundary.current().root
        self._workspace = workspace_root.resolve()
        self._skill_loader = SkillLoader(self._workspace, additional_paths=skill_paths)
        self._artifact_store = ArtifactStore(
            self._workspace / ".coderook" / "artifacts"
        )
        self._rehydrate()

    # 即时更新同一 Core 下后续消息的批量交付方式
    def set_follow_up_mode(self, mode: Literal["one-at-a-time", "all"]) -> None:
        self._follow_up_mode = mode

    # 读取新会话应继承的当前模型，不因未配置 Provider 阻止浏览会话。
    def _default_model_selection(self) -> tuple[str, str, ThinkingLevel]:
        if self._route_registry is None:
            return "", "", "off"
        try:
            route = self._route_registry.route()
        except (RouteResolutionError, ValueError):
            return "", "", "off"
        return route.id, route.model, route.thinking

    # 按会话优先解析 Python 扩展 Provider，否则使用共享 Provider Catalog。
    def _resolve_model_route(
        self,
        route_id: str | None,
        model: str | None,
        host: ExtensionHost | None,
    ) -> ResolvedRoute:
        provider = host.api.providers.get(route_id) if host is not None and route_id else None
        if provider is not None and provider.models:
            return provider.resolve(model or "")
        if self._route_registry is None:
            raise RouteResolutionError("provider routes are unavailable")
        base = self._route_registry.resolve(route_id, model=model)
        if host is None:
            return base
        override = provider
        if override is None:
            catalog_id = base.route.catalog_id or ""
            override = host.api.providers.get(catalog_id)
        if override is None:
            return base
        return override.resolve(model or "", base=base)

    # 用会话级思考强度覆盖已解析路由，不修改共享 Provider Catalog。
    @staticmethod
    def _apply_session_thinking(session: Session, resolved: ResolvedRoute) -> ResolvedRoute:
        level = session.thinking_level
        if level is None or level == resolved.route.thinking:
            return resolved
        update: dict[str, object] = {"thinking": level}
        if resolved.route.wire_format == "anthropic_messages" and level != "off":
            update["temperature"] = 1.0
        return replace(resolved, route=resolved.route.model_copy(update=update))

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
            reserved_waits = 0
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
                    reserved_waits += 1
                    if reserved_waits >= _MAX_RESERVED_GOAL_WAITS:
                        self._goal_service.set_status(
                            goal_id,
                            "blocked",
                            reason="stale token budget reservation requires repair",
                            actor="system",
                        )
                        return
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

    # 校验图片 artifact 元数据并构造可重放的多模态用户消息块。
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
        normalized = await normalize_tool_images(
            ToolResult("\n".join(descriptions), images=blocks),
            auto_resize=self._image_auto_resize,
        )
        return normalized.content, normalized.images or []

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
            for session in sessions:
                events = await asyncio.to_thread(self._store.read_session_events, session.id)
                for event in events:
                    if event.type == "session.auxiliary_usage":
                        await self._runtime.record_auxiliary_usage(
                            session.id, event.payload, event.seq,
                        )
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
            self._runtime_bootstrapped = True

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
        route_id, model, thinking_level = self._default_model_selection()
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
            route_id=route_id,
            model=model,
            thinking_level=thinking_level,
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._pending_plans_loaded.add(sid)
        self._store.write_meta(session)
        if self._runtime is not None:
            await self._runtime.sync_session(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        host = await self.prepare_extensions(sid)
        if host is not None:
            await host.emit_session_event({"type": "session_start", "reason": "new"})
            await self._discover_extension_resources(sid, host, reason="startup")
        return session

    # 为指定会话持久选择模型，后续 Turn 不再随其他会话的全局选择漂移。
    async def set_model(self, sid: str, route_id: str, model: str = "") -> Session:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "model cannot change during an active turn")
        host = await self.prepare_extensions(sid)
        try:
            resolved = self._resolve_model_route(route_id, model or None, host)
            route = resolved.route
        except (RouteResolutionError, ValueError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        selected_model = route.model
        previous = {"route_id": session.route_id, "model": session.model}
        async with lock:
            session.route_id = route.id
            session.model = selected_model
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
        if host is not None:
            await host.emit_session_event({
                "type": "model_select",
                "model": {
                    "provider": route.catalog_id or route.provider,
                    "route_id": route.id,
                    "id": selected_model,
                    "wire_format": route.wire_format,
                },
                "previousModel": previous if previous["route_id"] else None,
                "source": "set",
            })
        return session

    # 持久设置当前会话的思考强度，下一轮创建 Provider 时再应用。
    async def set_thinking(self, sid: str, thinking_level: ThinkingLevel) -> Session:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "thinking level cannot change during an active turn")
        async with lock:
            session.thinking_level = thinking_level
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
        host = await self.prepare_extensions(sid)
        if host is not None:
            await host.emit_session_event({
                "type": "thinking_level_select",
                "thinkingLevel": thinking_level,
                "source": "set",
            })
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
                    if event.type == "session.navigated":
                        pending = None
                    elif event.type == "plan.ready" and event.turn_id:
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
            if decision == "approve" and self._next_turn_workspace_edit_approver is not None:
                self._next_turn_workspace_edit_approver(sid)
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
        model_tools: Sequence[str] | None = None,
        expand_prompt_templates: bool = True,
        input_processed: bool = False,
        input_source: Literal["interactive", "rpc", "extension"] = "interactive",
    ) -> QueuedMessageRecord | None:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        if session.status == "closed":
            raise HandlerError(SESSION_CLOSED, "session already closed")
        if self._runtime is None:
            raise HandlerError(INVALID_PARAMS, "durable message queue is unavailable")
        if not input_processed:
            processed = await self.process_input(
                sid, content, attachments, source=input_source, streaming_behavior="follow_up",
            )
            if processed is None:
                return None
            content, attachments = processed
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
            expand_prompt_templates=expand_prompt_templates,
            attachments=attachments or [],
            tools=list(model_tools) if model_tools is not None else None,
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
            if sid in self._queue_paused:
                return
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
                    expand_prompt_templates=record.expand_prompt_templates,
                    input_processed=True,
                    model_tools=record.tools,
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
        expand_prompt_templates: bool = True,
        input_processed: bool = False,
        input_source: Literal["interactive", "rpc", "extension"] = "interactive",
        extension_custom_message: dict[str, Any] | None = None,
        model_tools: Sequence[str] | None = None,
    ) -> str:
        if not input_processed:
            processed = await self.process_input(sid, content, attachments, source=input_source)
            if processed is None:
                return ""
            content, attachments = processed
        resolved_run_id = run_id or new_run_id()
        await self.preflight_turn_start(sid, resolved_run_id)
        try:
            async with self._workspace_mutation_guard.turn():
                return await self._send_message(
                    sid,
                    content,
                    run_id=resolved_run_id,
                    runtime_mode=runtime_mode,
                    attachments=attachments,
                    display_content=display_content,
                    expand_prompt_templates=expand_prompt_templates,
                    extension_custom_message=extension_custom_message,
                    model_tools=model_tools,
                )
        finally:
            async with self._turn_reservation_lock:
                if self._turn_reservations.get(sid) == resolved_run_id:
                    self._turn_reservations.pop(sid, None)

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
        expand_prompt_templates: bool = True,
        extension_custom_message: dict[str, Any] | None = None,
        model_tools: Sequence[str] | None = None,
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

            shell_request = parse_user_shell(content) if expand_prompt_templates else None
            active_goal_candidate: GoalRecord | None = None
            active_goal_authority: AuthoritySnapshot | None = None
            if self._goal_service is not None and shell_request is None:
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

            extension_host = await self.prepare_extensions(sid)
            resolved_route: ResolvedRoute | None = None
            has_extension_route = bool(
                extension_host is not None
                and session.route_id in extension_host.api.providers
            )
            if shell_request is None and (
                self._route_registry is not None or has_extension_route
            ):
                try:
                    resolved_route = self._resolve_model_route(
                        session.route_id or None,
                        session.model or None,
                        extension_host,
                    )
                    resolved_route = self._apply_session_thinking(session, resolved_route)
                except (RouteResolutionError, ValueError) as exc:
                    raise HandlerError(INVALID_PARAMS, str(exc)) from exc
                if not session.route_id or not session.model:
                    session.route_id = resolved_route.route.id
                    session.model = resolved_route.route.model
                    self._store.write_meta(session)
                    if self._runtime is not None:
                        await self._runtime.sync_session(session)
            image_attachments = attachments or []
            attachment_text, image_blocks = await self._prepare_image_attachments(
                image_attachments
            )
            ledger_content = (
                f"{content.rstrip()}\n\n{attachment_text}".strip()
                if attachment_text
                else content
            )
            expanded_input = content
            if content.startswith("/"):
                try:
                    expanded_input = (
                        self._expand_native_skill(sid, content)
                        if expand_prompt_templates else content
                    )
                    ledger_content = (
                        f"{expanded_input}\n\n{attachment_text}"
                        if attachment_text else expanded_input
                    )
                except (SkillError, OSError) as exc:
                    raise HandlerError(INVALID_PARAMS, str(exc)) from exc
                display_content = display_content or content

            assert run_id is not None
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
            # Skill 与模板已作为用户消息展开，保持当前 Agent 系统提示和工具目录。
            goal = ledger_content

            runner = self._runner_factory()
            if resolved_route is not None:
                from code_rook.core.runner import AgentRunner

                accumulated_cost = (
                    await self._runtime.get_thread_estimated_cost(sid)
                    if self._runtime is not None
                    else 0.0
                )
                binding_options: dict[str, Any] = {
                    "resolved_route": resolved_route,
                    "runtime_mode": runtime_mode,
                    "run_id": run_id,
                    "accumulated_cost_usd": accumulated_cost,
                }
                if isinstance(runner, AgentRunner):
                    binding_options["resolved_route_is_explicit"] = bool(session.route_id)
                resolved_route = await runner.resolve_turn_binding(
                    **binding_options,
                )
                if resolved_route is not None:
                    resolved_route = self._apply_session_thinking(session, resolved_route)
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
                "runtime_mode": runtime_mode,
            }
            from code_rook.core.runner import AgentRunner

            if isinstance(runner, AgentRunner):
                host = extension_host
                if host is None:
                    host = runner.create_extension_host(run_id)
                    await host.initialize()
                    self._extension_hosts[sid] = host
                if sid not in self._extension_resources_ready:
                    await host.emit_session_event({"type": "session_start", "reason": "resume"})
                    await self._discover_extension_resources(sid, host, reason="startup")
                self._bind_extension_messages(sid, host, runtime_mode)
                run_options["extension_host"] = host
                run_options["skill_loader"] = self._skill_loader_for(sid)
                run_options["model_tools"] = model_tools
            if persistent_goal_context:
                run_options["persistent_goal_context"] = persistent_goal_context
            if resolved_route is not None:
                run_options["resolved_route"] = resolved_route
                run_options["resolved_route_is_explicit"] = True

            persistence_ready = asyncio.Event()

            # 普通同模式消息交给原生外层循环；需重建配置的输入仍按新运行派发。
            async def next_follow_up_message() -> list[FollowUpMessage]:
                runtime = self._runtime
                if runtime is None or sid in self._queue_paused:
                    return []
                record = await runtime.claim_next_queued_message(sid, datetime.now(UTC))
                if record is None:
                    return []
                current_model_tools = (
                    list(model_tools) if model_tools is not None else None
                )
                if record.mode != runtime_mode or record.tools != current_model_tools or (
                    record.expand_prompt_templates and parse_user_shell(record.content) is not None
                ):
                    await runtime.defer_queued_message(record, datetime.now(UTC))
                    return []
                try:
                    expanded_content = (self._expand_native_skill(sid, record.content)
                                        if record.expand_prompt_templates else record.content)
                except (SkillError, OSError) as exc:
                    await runtime.block_queued_message(record, str(exc), datetime.now(UTC))
                    return []
                if (record.expand_prompt_templates and record.content.startswith("/")
                        and not record.content.startswith("/skill:")
                        and expanded_content == record.content):
                    await runtime.defer_queued_message(record, datetime.now(UTC))
                    return []
                follow_up_content: str | list[dict[str, Any]] = expanded_content
                if record.attachments:
                    if resolved_route is not None and not resolved_route.route.supports_images:
                        await runtime.block_queued_message(
                            record, "selected Turn route does not support images", datetime.now(UTC)
                        )
                        return []
                    try:
                        description, images = await self._prepare_image_attachments(
                            record.attachments
                        )
                    except HandlerError as exc:
                        await runtime.block_queued_message(record, str(exc), datetime.now(UTC))
                        return []
                    follow_up_content = [
                        {"type": "text", "text": f"{expanded_content}\n\n{description}"}, *images,
                    ]
                if self._hooks is not None:
                    decision = await self._hooks.emit(
                        "message_submit",
                        {"session_id": sid, "run_id": run_id, "content": record.content},
                    )
                    if decision.blocked:
                        await runtime.block_queued_message(
                            record, decision.reason or "message blocked by hook", datetime.now(UTC)
                        )
                        return []

                # 先由循环持久化用户输入，再移除队列并通知前端，取消不丢尚未入账的内容。
                async def admitted() -> None:
                    await self._bus.publish(RunSteeredEvent(
                        run_id=run_id, session_id=sid,
                        content=record.display_content or record.content, ts=_now(),
                    ))
                    await runtime.remove_queued_message(
                        sid, record.id, reason="dispatched", ts=datetime.now(UTC)
                    )

                return [FollowUpMessage(follow_up_content, admitted)]

            # 按 Pi 的交付模式领取一条或当前全部后续消息，遇独立命令时保留队列顺序
            async def next_follow_up() -> list[FollowUpMessage]:
                messages = await next_follow_up_message()
                if self._follow_up_mode == "all":
                    while messages:
                        additional = await next_follow_up_message()
                        if not additional:
                            break
                        messages.extend(additional)
                return messages

            # 将扩展输入送入本会话的现有队列，保留当前运行模式与停止恢复语义
            async def send_extension_message(
                content: UserMessageContent, deliver_as: Literal["steer", "follow_up"],
                expand: bool,
            ) -> None:
                host = self._extension_hosts.get(sid)
                if host is None:
                    raise RuntimeError("Extension messaging requires a session-owned host")
                await host.api.send_user_message(
                    content, deliver_as=deliver_as, expand_prompt_templates=expand,
                )

            # 让可取消 runner 在所有 session/runtime 写入完成前停在内存屏障
            async def execute_after_persistence() -> Any:
                await persistence_ready.wait()
                if shell_request is not None:
                    return await runner.run_user_shell(
                        shell_request, run_id=run_id, session=session, store=self._store,
                        runtime_mode=runtime_mode,
                        extension_host=run_options.get("extension_host"),
                    )
                if self._interaction_manager is not None:
                    self._interaction_manager.bind_follow_up(run_id, next_follow_up)
                    self._interaction_manager.bind_extension_sender(run_id, send_extension_message)
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
                for queued_extension in self._next_turn_extension_messages.pop(sid, []):
                    await self._append_extension_custom_message(sid, queued_extension)
                if shell_request is not None:
                    self._store.append_session_event(
                        sid, event_type="user.shell_requested",
                        payload={"command": shell_request.command}, turn_id=run_id,
                    )
                elif extension_custom_message is not None:
                    self._record_extension_custom_message(
                        sid,
                        extension_custom_message,
                        run_id=run_id,
                    )
                else:
                    self._store.append_message(
                        sid,
                        "user",
                        [{"type": "text", "text": ledger_content}, *image_blocks]
                        if image_blocks else ledger_content,
                        run_id=run_id,
                        message_id=f"{run_id}:user",
                        display_content=display_content,
                    )
                await self._bus.publish(
                    SessionMessageReceivedEvent(
                        session_id=sid,
                        content=display_content or ledger_content,
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
                if expanded_input.startswith("<skill name="):
                    skill_parts = content.removeprefix("/skill:").removeprefix("/").split(None, 1)
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_parts[0],
                            arguments=skill_parts[1] if len(skill_parts) > 1 else "",
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
                if self._interaction_manager is not None:
                    self._interaction_manager.unbind_follow_up(run_id)
                self._active_runs.pop(run_id, None)
                active.finished.set()
                for pending_extension in self._pending_extension_messages.pop(sid, []):
                    await self._append_extension_custom_message(sid, pending_extension)

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
                    TurnStatus.INTERRUPTED
                    if outcome.reason == "cancelled"
                    else TurnStatus.COMPLETED
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
        return sid in self._turn_reservations or self._locks[sid].locked() or any(
            continuation.session_id == sid and not continuation.task.done()
            for continuation in self._goal_continuations.values()
        )

    # 在异步派发前检查常见拒绝状态，避免 IPC 先返回成功再由后台 task 静默失败
    async def preflight_turn_start(self, sid: str, run_id: str) -> None:
        await self._ensure_runtime_sessions()
        async with self._turn_reservation_lock:
            session = self._get_session(sid)
            current_task = asyncio.current_task()
            reservation = self._turn_reservations.get(sid)
            if reservation is not None and reservation != run_id:
                raise HandlerError(SESSION_BUSY, "session busy")
            if self._locks[sid].locked() or any(
                continuation.session_id == sid and not continuation.task.done()
                and continuation.task is not current_task
                for continuation in self._goal_continuations.values()
            ):
                raise HandlerError(SESSION_BUSY, "session busy")
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")
            if await self._load_pending_plan(sid) is not None:
                raise HandlerError(
                    INVALID_PARAMS,
                    "pending plan must be approved, revised, or cancelled before a new turn",
                )
            self._turn_reservations[sid] = run_id

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

    # 停止当前运行并保留未发送消息，避免停止后队列立即重新启动任务。
    async def cancel_run(self, run_id: str) -> str:
        active = self._active_runs.get(run_id)
        if active is None or active.task.done():
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        sid = active.session_id
        self._queue_paused.add(sid)
        pending = (
            self._interaction_manager.take_pending_steering(run_id)
            if self._interaction_manager is not None else []
        )
        if not active.task.cancel():
            self._queue_paused.discard(sid)
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        try:
            try:
                await active.task
            except asyncio.CancelledError:
                pass
            if self._runtime is not None:
                for content in pending:
                    text, images = await self._extension_input(content)
                    await self.queue_message(
                        sid, text, attachments=images, expand_prompt_templates=False,
                        input_processed=True,
                    )
                for record in await self._runtime.list_queued_messages(sid):
                    if record.status in {"queued", "dispatching"}:
                        await self._runtime.block_queued_message(
                            record, "Run stopped; resend this pending message to continue.",
                            datetime.now(UTC),
                        )
            await active.finished.wait()
        finally:
            self._queue_paused.discard(sid)
        if self._subagent_registry is not None:
            await self._subagent_registry.cancel_descendants(run_id)
        await active.finished.wait()
        return active.session_id

    # 将用户新指令排入活动 run，在下一次模型决策前注入
    async def steer_run(
        self, run_id: str, content: str, *, expand_prompt_templates: bool = True,
        attachments: list[ImageArtifactInput] | None = None,
        input_source: Literal["interactive", "rpc", "extension"] = "interactive",
    ) -> str:
        active = self._active_runs.get(run_id)
        if active is None or active.task.done() or self._interaction_manager is None:
            raise HandlerError(RUN_NOT_ACTIVE, "run is not active")
        processed = await self.process_input(
            active.session_id, content, attachments,
            source=input_source, streaming_behavior="steer",
        )
        if processed is None:
            return active.session_id
        content, attachments = processed
        try:
            expanded = (self._expand_native_skill(active.session_id, content)
                        if active and expand_prompt_templates else content)
        except (SkillError, OSError) as exc:
            raise HandlerError(INVALID_PARAMS, str(exc)) from exc
        admitted: UserMessageContent = expanded
        if attachments:
            if self._runtime is not None:
                turn = await self._runtime.get_turn(run_id)
                if turn.route is not None and not turn.route.supports_images:
                    raise HandlerError(
                        INVALID_PARAMS,
                        "selected Turn route does not support images; "
                        "select an image-capable route",
                    )
            description, images = await self._prepare_image_attachments(attachments)
            admitted = [{"type": "text", "text": f"{expanded}\n\n{description}"}, *images]
        if active.task.done() or not self._interaction_manager.steer(run_id, admitted):
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

    # 统一首次输入、纠偏和后续消息的 Skill 展开行为。
    def _expand_native_skill(self, sid: str, content: str) -> str:
        trusted = (
            self._authority_provider is not None
            and self._authority_provider(sid).workspace_trust == WorkspaceTrust.TRUSTED
        )
        skill_loader = self._skill_loader_for(sid)
        expanded = expand_skill_input(content, skill_loader, workspace_trusted=trusted)
        directories = self._prompt_directories(sid, workspace_trusted=trusted)
        expanded = expand_prompt_template(expanded, directories)
        if expanded == content and content.startswith("/") and not content.startswith("/skill:"):
            parts = content[1:].split(None, 1)
            if parts:
                # 旧名称命令仅作为 Skill 语法别名，不再覆写系统提示。
                expanded = expand_skill_input(
                    f"/skill:{content[1:]}", skill_loader, workspace_trusted=trusted
                )
                if expanded == f"/skill:{content[1:]}":
                    return content
        return expanded

    # 停止所有会话工作并释放会话拥有的扩展资源
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
        if run_ids:
            await asyncio.gather(
                *(self.cancel_run(run_id) for run_id in run_ids),
                return_exceptions=True,
            )
        hosts, self._extension_hosts = self._extension_hosts, {}
        self._extension_skill_loaders.clear()
        self._extension_prompt_paths.clear()
        self._extension_theme_paths.clear()
        self._extension_resources_ready.clear()
        for host in hosts.values():
            await host.emit_session_event({"type": "session_shutdown", "reason": "quit"})
            await host.close()

    # 提取扩展自定义消息的模型可见文本，非文本消息保留明确的来源占位。
    def _extension_custom_text(self, message: dict[str, Any]) -> str:
        content = message.get("content", [])
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            if text:
                return text
        return f"[Extension message: {message.get('customType', 'message')}]"

    # 将扩展自定义消息追加到会话事实日志，并返回持久序号。
    def _record_extension_custom_message(
        self,
        sid: str,
        message: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> int:
        event = self._store.append_session_event(
            sid,
            event_type="input.admitted",
            turn_id=run_id or "",
            payload={
                "role": "user",
                "content": deepcopy(message.get("content", [])),
                "message_id": (
                    f"{run_id or sid}:extension:{uuid.uuid4().hex[:12]}"
                ),
                "source": {
                    "kind": "extension",
                    "custom_type": str(message.get("customType", "message")),
                },
                "display": bool(message.get("display", False)),
                "details": deepcopy(message.get("details")),
            },
        )
        return event.seq

    # 将可见扩展消息发布到持久运行时流，隐藏消息仅保留在模型事实日志。
    async def _publish_extension_custom_message(
        self,
        sid: str,
        message: dict[str, Any],
        ledger_seq: int,
    ) -> None:
        if not message.get("display", False):
            return
        content = message.get("content", [])
        blocks = content if isinstance(content, list) else [
            {"type": "text", "text": str(content)},
        ]
        await self._bus.publish(AgentMessageEvent(
            run_id=f"extension:{sid}:{uuid.uuid4().hex[:12]}",
            session_id=sid,
            message_id=f"{sid}:extension:{ledger_seq}",
            phase="end",
            role="custom",
            custom_type=str(message.get("customType", "message")),
            content=deepcopy(blocks),
            ledger_seq=ledger_seq,
            ts=_now(),
        ))

    # 追加并按展示标记发布一条空闲扩展消息。
    async def _append_extension_custom_message(
        self,
        sid: str,
        message: dict[str, Any],
    ) -> int:
        ledger_seq = self._record_extension_custom_message(sid, message)
        await self._publish_extension_custom_message(sid, message, ledger_seq)
        return ledger_seq

    # 将扩展文本与内嵌图片转成普通会话输入及内容寻址附件
    async def _extension_input(
        self, content: UserMessageContent,
    ) -> tuple[str, list[ImageArtifactInput]]:
        if isinstance(content, str):
            if not content.strip():
                raise ValueError("Extension message must not be blank")
            return content, []
        if isinstance(content, dict):
            raise ValueError("User extension messages must contain text or image blocks")
        texts: list[str] = []
        attachments: list[ImageArtifactInput] = []
        for block in content:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "image":
                source = block.get("source", block)
                data = base64.b64decode(source["data"], validate=True)
                if not data or len(data) > 2 * 1024 * 1024:
                    raise ValueError("image must contain between 1 byte and 2 MiB")
                metadata = inspect_image(data)
                reference = await self._artifact_store.put(data, media_type=metadata.media_type)
                attachments.append(ImageArtifactInput(
                    sha256=reference.sha256, media_type=metadata.media_type,
                    size=reference.size, width=metadata.width, height=metadata.height,
                ))
            else:
                raise ValueError("Extension message supports only text and image blocks")
        text = "\n".join(texts)
        if not text.strip() and not attachments:
            raise ValueError("Extension message must not be blank")
        return text if text.strip() else "[Image attachment]", attachments

    # 让扩展消息跟随会话当前活动任务，空闲时启动该会话的下一轮
    def _bind_extension_messages(
        self, sid: str, host: ExtensionHost, mode: RuntimeMode,
    ) -> None:
        # 每次交付查找当前运行，不能捕获上一轮已经结束的 run ID
        async def send_message(
            content: UserMessageContent, deliver_as: Literal["steer", "follow_up"], expand: bool,
        ) -> None:
            text, attachments = await self._extension_input(content)
            active_id = next((run_id for run_id, run in self._active_runs.items()
                              if run.session_id == sid and not run.task.done()), None)
            if active_id is not None:
                if deliver_as == "steer":
                    await self.steer_run(
                        active_id, text, expand_prompt_templates=expand,
                        attachments=attachments, input_source="extension",
                    )
                else:
                    await self.queue_message(
                        sid, text, runtime_mode=mode, attachments=attachments,
                        expand_prompt_templates=expand,
                        input_source="extension",
                    )
            else:
                await self.send_message(
                    sid, text, runtime_mode=mode, attachments=attachments,
                    expand_prompt_templates=expand,
                    input_source="extension",
                )

        host.api.message_sender = send_message

        # 按 Pi 的 triggerTurn 和 deliverAs 语义交付带来源的扩展消息。
        async def send_custom_message(
            message: dict[str, Any],
            trigger_turn: bool | None,
            deliver_as: Literal["steer", "follow_up", "next_turn"] | None,
        ) -> None:
            if deliver_as == "next_turn":
                self._next_turn_extension_messages.setdefault(sid, []).append(
                    deepcopy(message)
                )
                return
            active_id = self.active_run_id(sid)
            if active_id is not None:
                if trigger_turn is not False and deliver_as != "follow_up":
                    if (
                        self._interaction_manager is None
                        or not self._interaction_manager.steer(active_id, message)
                    ):
                        raise RuntimeError("Extension could not steer the active run")
                    return
                if trigger_turn is not False and deliver_as == "follow_up":
                    if (
                        self._interaction_manager is None
                        or not self._interaction_manager.queue_extension_follow_up(
                            active_id, message
                        )
                    ):
                        raise RuntimeError("Extension could not queue the active run message")
                    return
                self._pending_extension_messages.setdefault(sid, []).append(
                    deepcopy(message)
                )
                return
            if trigger_turn:
                await self.send_message(
                    sid,
                    self._extension_custom_text(message),
                    runtime_mode=mode,
                    input_processed=True,
                    input_source="extension",
                    extension_custom_message=message,
                )
                return
            await self._append_extension_custom_message(sid, message)

        host.api.custom_message_sender = send_custom_message

        # 将扩展通知发布给当前会话前端，不把临时提示混入模型上下文。
        async def send_notification(
            message: str,
            severity: Literal["info", "warning", "error"],
        ) -> None:
            await self._bus.publish(ExtensionNotificationEvent(
                run_id=f"extension:{sid}:{uuid.uuid4().hex[:12]}",
                session_id=sid,
                message=message,
                severity=severity,
                ts=_now(),
            ))

        host.api.notification_sender = send_notification

        # 把扩展 UI contribution 保存为会话投影，并通过持久事件同步到所有前端。
        def set_ui(kind: ExtensionUiKind, key: str, value: Any) -> None:
            state = self._extension_ui.setdefault(sid, {
                "statuses": {},
                "widgets": {},
                "working_message": None,
                "working_visible": True,
                "hidden_thinking_label": None,
                "title": None,
                "tools_expanded": False,
            })
            if kind == "status":
                statuses = state["statuses"]
                if value is None:
                    statuses.pop(key, None)
                else:
                    statuses[key] = value
            elif kind == "widget":
                widgets = state["widgets"]
                if value is None:
                    widgets.pop(key, None)
                else:
                    widgets[key] = value
            elif kind not in {"editor_text", "editor_insert"}:
                state[kind] = value
            asyncio.get_running_loop().create_task(self._bus.publish(ExtensionUiUpdatedEvent(
                run_id=f"extension:{sid}:{uuid.uuid4().hex[:12]}",
                session_id=sid,
                kind=kind,
                key=key,
                value=deepcopy(value),
                ts=_now(),
            )))

        host.api.ui_setter = set_ui

        # 让扩展命令切换当前会话模型，不修改其他会话或全局默认值
        async def set_model(provider: str, model: str) -> None:
            await self.set_model(sid, provider, model)

        host.api.model_setter = set_model

        # 返回当前会话的模型选择摘要，供扩展状态栏和命令自省使用。
        def get_model() -> dict[str, Any] | None:
            session = self._get_session(sid)
            if not session.route_id and not session.model:
                return None
            return {
                "provider": session.route_id,
                "id": session.model,
                "thinking": session.thinking_level or "off",
            }

        host.api.model_getter = get_model

        # 返回会话显式思考档位；未设置时按关闭展示，实际请求仍继承路由。
        def get_thinking_level() -> ThinkingLevel:
            return self._get_session(sid).thinking_level or "off"

        # 让扩展复用正式会话思考档位操作。
        async def set_thinking_level(level: ThinkingLevel) -> None:
            await self.set_thinking(sid, level)

        host.api.thinking_getter = get_thinking_level
        host.api.thinking_setter = set_thinking_level

        # 让扩展追加不进入模型上下文的持久状态条目，并返回稳定账本序号。
        def append_entry(custom_type: str, data: Any) -> int:
            event = self._store.append_session_event(
                sid,
                event_type="extension.entry",
                payload={"custom_type": custom_type, "data": data},
                provenance="extension",
            )
            return event.seq

        # 返回当前内存中的会话标题，避免扩展自行读取 meta.json。
        def get_session_name() -> str | None:
            return self._get_session(sid).title or None

        # 复用正式重命名操作，使 TUI、Web 与 Runtime 投影同步刷新。
        async def set_session_name(name: str) -> None:
            await self.rename(sid, name)

        # 将扩展书签限制在当前会话已经存在的稳定账本条目。
        def set_entry_label(entry_id: str, label: str | None) -> None:
            try:
                sequence = int(entry_id)
            except ValueError as exc:
                raise ValueError("entry_id must be a ledger sequence") from exc
            self._store.set_entry_label(sid, sequence, label)

        host.api.entry_appender = append_entry
        host.api.session_name_getter = get_session_name
        host.api.session_name_setter = set_session_name
        host.api.entry_label_setter = set_entry_label

        # 扩展按会话查询实际活动任务，不依赖宿主加载时的旧 run ID。
        def is_idle() -> bool:
            return self.active_run_id(sid) is None

        # 汇总内存纠偏、扩展消息和持久派发任务，供扩展决定是否继续排队。
        def has_pending_messages() -> bool:
            run_id = self.active_run_id(sid)
            interaction_pending = (
                run_id is not None
                and self._interaction_manager is not None
                and self._interaction_manager.has_pending_messages(run_id)
            )
            dispatcher = self._queue_dispatch_tasks.get(sid)
            return bool(
                interaction_pending
                or self._pending_extension_messages.get(sid)
                or self._next_turn_extension_messages.get(sid)
                or dispatcher is not None and not dispatcher.done()
            )

        # 等待当前活动任务的 finished 屏障，避免只等 asyncio Task 返回。
        async def wait_for_idle() -> None:
            run_id = self.active_run_id(sid)
            if run_id is None:
                return
            active = self._active_runs.get(run_id)
            if active is not None:
                await active.finished.wait()

        # 取消当前会话活动任务并保留尚未消费的纠偏与后续消息。
        async def abort_run() -> bool:
            run_id = self.active_run_id(sid)
            if run_id is None:
                return False
            await self.cancel_run(run_id)
            return True

        # 等待当前会话完成后转交 Core 的有序退出信号。
        async def request_shutdown() -> None:
            if self._shutdown_requester is None:
                raise RuntimeError("Core shutdown is not available")
            await wait_for_idle()
            result = self._shutdown_requester()
            if inspect.isawaitable(result):
                await result

        # 复用正式压缩入口，确保摘要与 Ledger 事件语义一致。
        async def compact_session(focus: str) -> Any:
            return await self.compact(sid, focus)

        # 复用正式资源重载入口，使前端命令目录同步更新。
        async def reload_session() -> dict[str, Any]:
            return await self.reload_resources(sid)

        # 暴露当前有效 Prompt 的基础输入快照，不返回 Provider 凭据。
        def system_prompt_options() -> dict[str, Any]:
            trusted = (
                self._authority_provider is not None
                and self._authority_provider(sid).workspace_trust == WorkspaceTrust.TRUSTED
            )
            return {
                "system_prompt": host.api.get_system_prompt(),
                "active_tools": (
                    host.api.get_active_tools() if host.api.registry is not None else []
                ),
                "cwd": str(self._workspace),
                "skills": [
                    skill.name
                    for skill in self._skill_loader_for(sid).list_for_execution(
                        workspace_trusted=trusted
                    )
                ],
            }

        # 从扩展命令创建新会话，并保留显式或当前父会话关系。
        async def create_session(options: dict[str, Any]) -> dict[str, Any]:
            if not is_idle():
                raise HandlerError(
                    SESSION_BUSY, "wait for the active turn before creating a session"
                )
            decision = await host.emit_session_event({
                "type": "session_before_switch",
                "reason": "new",
            })
            if decision is not None and decision.get("cancel") is True:
                return {"cancelled": True, "session_id": sid}
            source = self._get_session(sid)
            created = await self.create(
                "chat",
                str(options.get("title", "")),
                preset_id=str(options.get("preset_id", source.preset_id)),
            )
            created.route_id = source.route_id
            created.model = source.model
            created.thinking_level = source.thinking_level
            parent_reference = options.get("parent_session")
            created.parent_session_id = (
                None
                if parent_reference is None
                else self._session_id_from_reference(str(parent_reference))
            )
            self._store.write_meta(created)
            if self._runtime is not None:
                await self._runtime.sync_session(created)
            created_host = await self.prepare_extensions(created.id)
            if created_host is not None:
                for callback_name in ("setup", "with_session"):
                    callback = options.get(callback_name)
                    if not callable(callback):
                        continue
                    callback_result = callback(created_host.api)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            return {"cancelled": False, "session_id": created.id}

        # 把扩展的 before/at 位置换算为现有 Ledger 分支节点后创建 Fork。
        async def fork_session(entry_id: str, options: dict[str, Any]) -> dict[str, Any]:
            try:
                target = int(entry_id)
            except ValueError as exc:
                raise ValueError("entry_id must be a ledger sequence") from exc
            position = options.get("position", "at")
            leaf = target
            if position == "before":
                item = next(
                    (entry for entry in self._store.session_tree(sid) if entry["seq"] == target),
                    None,
                )
                if item is None:
                    raise ValueError("session entry does not exist")
                leaf = int(item["parent_seq"] or 0)
            try:
                forked = await self.fork(sid, leaf_seq=leaf, position=str(position))
            except HandlerError as exc:
                if exc.code == INVALID_PARAMS and "cancelled by extension" in str(exc):
                    return {"cancelled": True, "session_id": sid}
                raise
            callback = options.get("with_session")
            if callable(callback):
                forked_host = await self.prepare_extensions(forked.id)
                if forked_host is not None:
                    callback_result = callback(forked_host.api)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            return {"cancelled": False, "session_id": forked.id}

        # 复用正式树导航，确保摘要、标签和持久事件与 TUI/Web 完全一致。
        async def navigate_session(target_id: str, options: dict[str, Any]) -> dict[str, Any]:
            try:
                target = int(target_id)
            except ValueError as exc:
                raise ValueError("target_id must be a ledger sequence") from exc
            try:
                result = await self.navigate_tree(
                    sid,
                    target,
                    summarize=bool(options.get("summarize", False)),
                    focus=str(options.get("custom_instructions", "")),
                    label=str(options.get("label", "")),
                )
                return {"cancelled": False, **result}
            except HandlerError as exc:
                if exc.code == INVALID_PARAMS and "cancelled by extension" in str(exc):
                    return {"cancelled": True, "session_id": sid}
                raise

        # 在扩展切换前允许生命周期钩子取消，再恢复目标会话并返回稳定标识。
        async def switch_session(
            reference: str, options: dict[str, Any],
        ) -> dict[str, Any]:
            target_sid = self._session_id_from_reference(reference)
            if target_sid == sid:
                return {"cancelled": False, "session_id": sid}
            if not is_idle():
                raise HandlerError(
                    SESSION_BUSY, "wait for the active turn before switching sessions"
                )
            decision = await host.emit_session_event({
                "type": "session_before_switch",
                "reason": "resume",
                "targetSessionId": target_sid,
                "targetSessionFile": str(
                    self._store.session_dir(target_sid) / "thread.jsonl"
                ),
            })
            if decision is not None and decision.get("cancel") is True:
                return {"cancelled": True, "session_id": sid}
            target = await self.resume(target_sid)
            callback = options.get("with_session")
            if callable(callback):
                target_host = await self.prepare_extensions(target.id)
                if target_host is not None:
                    callback_result = callback(target_host.api)
                    if inspect.isawaitable(callback_result):
                        await callback_result
            return {"cancelled": False, "session_id": target.id}

        host.api.idle_getter = is_idle
        host.api.pending_messages_getter = has_pending_messages
        host.api.idle_waiter = wait_for_idle
        host.api.run_aborter = abort_run
        host.api.shutdown_requester = request_shutdown
        host.api.compaction_requester = compact_session
        host.api.resource_reloader = reload_session
        host.api.system_prompt_options_getter = system_prompt_options
        host.api.session_creator = create_session
        host.api.session_forker = fork_session
        host.api.tree_navigator = navigate_session
        host.api.session_switcher = switch_session

        # 扩展 UI 问题复用正式 InteractionManager，活动与空闲会话使用同一响应通道。
        async def ask_extension_question(
            question: str, header: str, options: list[str], multi_select: bool,
        ) -> str:
            if self._interaction_manager is None:
                raise RuntimeError("Interactive prompts are not available")
            run_id = self.active_run_id(sid) or f"extension:{sid}:{uuid.uuid4().hex[:12]}"
            return await self._interaction_manager.ask(
                run_id=run_id,
                session_id=sid,
                question=question,
                header=header,
                options=options,
                multi_select=multi_select,
            )

        host.api.question_asker = ask_extension_question

    # 在模板展开和持久入队之前处理输入，返回空值表示扩展已经接管
    async def process_input(
        self, sid: str, content: str, attachments: list[ImageArtifactInput] | None = None, *,
        source: Literal["interactive", "rpc", "extension"] = "interactive",
        streaming_behavior: Literal["steer", "follow_up"] | None = None,
    ) -> tuple[str, list[ImageArtifactInput]] | None:
        session = self._get_session(sid)
        if session.status == "closed":
            raise HandlerError(SESSION_CLOSED, "session already closed")
        host = await self.prepare_extensions(sid)
        if host is None or not any(kind == "input" for kind, _ in host.api.handlers):
            return content, attachments or []
        _, images = await self._prepare_image_attachments(attachments or [])
        native_images = []
        for image in images:
            image_source = image["source"]
            assert isinstance(image_source, dict)
            native_images.append({"type": "image", "data": image_source["data"],
                                  "mimeType": image_source["media_type"]})
        result = await host.emit_input(
            content, native_images or None, source=source, streaming_behavior=streaming_behavior,
        )
        if result["action"] == "handled":
            return None
        if result["action"] == "transform":
            return await self._extension_input([
                {"type": "text", "text": result["text"]}, *(result.get("images") or []),
            ])
        return content, attachments or []

    # 首次打开会话时装载扩展命令，后续查询复用同一个宿主
    async def prepare_extensions(self, sid: str) -> ExtensionHost | None:
        self._get_session(sid)
        host = self._extension_hosts.get(sid)
        if host is not None and not host.api.closed:
            return host
        from code_rook.core.runner import AgentRunner

        runner = self._runner_factory()
        if not isinstance(runner, AgentRunner):
            return None
        host = runner.create_extension_host("")
        try:
            await host.initialize()
        except BaseException:
            await host.close()
            raise
        mode = (self._authority_provider(sid).mode
                if self._authority_provider is not None else RuntimeMode.ACT)
        self._bind_extension_messages(sid, host, mode)
        self._extension_hosts[sid] = host
        return host

    # 将扩展发现的临时资源冻结到当前会话，并让后续输入与模型工具共享同一 Skill 视图
    async def _discover_extension_resources(
        self,
        sid: str,
        host: ExtensionHost,
        *,
        reason: Literal["startup", "reload"],
    ) -> None:
        resources = await host.discover_resources(reason)
        skill_paths = resources["skill_paths"]
        if skill_paths:
            self._extension_skill_loaders[sid] = SkillLoader(
                self._workspace,
                additional_paths=(*self._skill_paths, *skill_paths),
            )
        else:
            self._extension_skill_loaders.pop(sid, None)
        prompt_paths = resources["prompt_paths"]
        if prompt_paths:
            self._extension_prompt_paths[sid] = prompt_paths
        else:
            self._extension_prompt_paths.pop(sid, None)
        theme_paths = resources["theme_paths"]
        if theme_paths:
            self._extension_theme_paths[sid] = theme_paths
        else:
            self._extension_theme_paths.pop(sid, None)
        self._extension_resources_ready.add(sid)

    # 返回会话冻结的扩展 Skill 目录，未发现扩展资源时复用基础加载器
    def _skill_loader_for(self, sid: str) -> SkillLoader:
        return self._extension_skill_loaders.get(sid, self._skill_loader)

    # 按用户、项目、配置和扩展顺序返回当前会话的 Prompt 模板路径
    def _prompt_directories(
        self, sid: str, *, workspace_trusted: bool,
    ) -> list[Path]:
        directories = [Path("~/.coderook/prompts").expanduser()]
        if workspace_trusted:
            directories.append(self._workspace / ".coderook" / "prompts")
        directories.extend(self._workspace / path.expanduser() for path in self._prompt_paths)
        directories.extend(self._extension_prompt_paths.get(sid, ()))
        return directories

    # 删除指定会话的扩展资源投影，防止重载、关闭或删除后继续暴露旧资源
    def _clear_extension_resources(self, sid: str) -> None:
        self._extension_skill_loaders.pop(sid, None)
        self._extension_prompt_paths.pop(sid, None)
        self._extension_theme_paths.pop(sid, None)
        self._extension_ui.pop(sid, None)
        self._extension_resources_ready.discard(sid)

    # 执行用户提交的扩展斜杠命令，命令处理器可选择再发送模型任务
    async def execute_extension_command(self, sid: str, content: str) -> str:
        host = await self.prepare_extensions(sid)
        parts = content.removeprefix("/").split(None, 1)
        if host is None or not parts:
            raise HandlerError(INVALID_PARAMS, "Unknown extension command")
        return await host.execute_command(parts[0], parts[1] if len(parts) > 1 else "")

    # 在会话空闲时重新读取扩展源码，释放旧状态且不启动模型任务
    async def reload_resources(self, sid: str) -> dict[str, Any]:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "Stop the current run before reloading extensions")
        previous = self._extension_hosts.get(sid)
        if previous is not None:
            await previous.emit_session_event({"type": "session_shutdown", "reason": "reload"})
        async with lock:
            previous = self._extension_hosts.pop(sid, None)
            self._clear_extension_resources(sid)
            if previous is not None:
                await previous.close()
            from code_rook.core.runner import AgentRunner

            runner = self._runner_factory()
            if isinstance(runner, AgentRunner):
                host = runner.create_extension_host("")
                try:
                    await host.initialize()
                except BaseException:
                    await host.close()
                    raise
                self._extension_hosts[sid] = host
                mode = (self._authority_provider(sid).mode
                        if self._authority_provider is not None else RuntimeMode.ACT)
                self._bind_extension_messages(sid, host, mode)
                await host.emit_session_event({"type": "session_start", "reason": "reload"})
                await self._discover_extension_resources(sid, host, reason="reload")
            return self.context_info(sid)

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        host = self._extension_hosts.get(sid)
        if host is not None:
            await host.emit_session_event({"type": "session_shutdown", "reason": "quit"})
        async with lock:
            if self._hooks is not None:
                await self._hooks.emit(
                    "session_stop",
                    {"session_id": sid, "reason": "closed"},
                )
            session.status = "closed"
            host = self._extension_hosts.pop(sid, None)
            self._clear_extension_resources(sid)
            self._pending_extension_messages.pop(sid, None)
            self._next_turn_extension_messages.pop(sid, None)
            if host is not None:
                await host.close()
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        host = await self.prepare_extensions(sid)
        from code_rook.core.bus.commands import SessionCompactResult

        messages = self._store.read_messages(sid)
        session_dir = self._store.session_dir(sid)
        config = self._compaction_config
        provider = self._provider
        if self._route_registry is not None or (host is not None and session.route_id):
            from code_rook.core.llm.factory import create_provider_for_resolved_route

            try:
                resolved_route = self._resolve_model_route(
                    session.route_id or None,
                    session.model or None,
                    host,
                )
                resolved_route = self._apply_session_thinking(session, resolved_route)
            except RouteResolutionError as exc:
                raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            provider = create_provider_for_resolved_route(resolved_route)
        if provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from code_rook.core.compact.compactor import Compactor
            from code_rook.core.context import ExecutionContext
            compactor = Compactor(
                self._bus, session_dir, sid, store=self._store,
                strategy=config.strategy, retain_ratio=config.retain_ratio,
                keep_recent_tokens=config.keep_recent_tokens, reserve_tokens=config.reserve_tokens,
                retry_policy=self._summary_retry_policy,
                lifecycle=host.emit_session_event if host is not None else None,
            )
            latest_run_id = session.run_ids[-1] if session.run_ids else ""
            compact_context = ExecutionContext(
                run_id=latest_run_id or "manual",
                goal="",
                max_steps=1,
                prefill_messages=messages,
            )
            result = await compactor.compact(
                compact_context, provider, focus=focus, trigger="manual",
            )
            if result is None:
                if compactor.skip_reason in {"not_needed", "not_beneficial"}:
                    estimated_tokens = estimate_messages_tokens(messages)
                    return SessionCompactResult(
                        status="not_needed",
                        message=(
                            "context already fits within the retained window"
                            if compactor.skip_reason == "not_needed"
                            else "compaction would not reduce the current context"
                        ),
                        summary_tokens=0,
                        saved_tokens=0,
                        original_tokens=estimated_tokens,
                        compacted_tokens=estimated_tokens,
                        retained_tokens=estimated_tokens,
                        retained_messages=len(messages),
                    )
                raise HandlerError(-32021, "compaction failed")
            if self._hooks is not None:
                await self._hooks.emit(
                    "compaction_completed",
                    {
                        "session_id": sid,
                        "run_id": latest_run_id or "manual",
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

    # 返回界面历史，包含不进入模型上下文的用户 Shell 记录
    async def get_display_history(self, sid: str) -> list[dict[str, Any]]:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        return self._store.derive_messages(sid, display=True)

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
        navigation = self._store.navigation_projection(sid)
        if navigation is None:
            navigation = self._store.import_projection(sid)
        return {
            "message_count": len(messages),
            "navigation": navigation,
            "input_commands": self.input_commands(sid),
            "theme_paths": [str(path) for path in self._extension_theme_paths.get(sid, ())],
            "extension_ui": deepcopy(self._extension_ui.get(sid, {})),
            "route_id": session.route_id,
            "model": session.model,
            "thinking_level": session.thinking_level,
            "extension_providers": (
                self._extension_hosts[sid].api.get_registered_providers()
                if sid in self._extension_hosts else []
            ),
            "estimated_tokens": estimate_messages_tokens(messages),
            "run_count": len(session.run_ids),
            "last_run_id": session.run_ids[-1] if session.run_ids else None,
            "memory_count": len(
                MemoryStore(WorkspaceBoundary.current().root / ".coderook" / "memory").list_all()
            ),
        }

    # 为双前端返回同一会话实际可执行的 Skill 与模板命令目录。
    def input_commands(self, sid: str = "") -> list[dict[str, str]]:
        if sid:
            self._get_session(sid)
        trusted = (
            self._authority_provider is not None
            and self._authority_provider(sid).workspace_trust == WorkspaceTrust.TRUSTED
        )
        directories = self._prompt_directories(sid, workspace_trusted=trusted)
        commands = list_prompt_templates(directories)
        commands.extend(
            {"name": f"skill:{skill.name}", "description": skill.description, "kind": "skill"}
            for skill in self._skill_loader_for(sid).list_for_execution(
                workspace_trusted=trusted
            )
        )
        host = self._extension_hosts.get(sid)
        if host is not None:
            names = set(host.commands)
            commands = [command for command in commands if command["name"] not in names]
            commands.extend({"name": command.name, "description": command.description,
                             "kind": "extension"} for command in host.commands.values())
        return commands

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

    # 将扩展传入的会话 ID 或 thread.jsonl 路径解析为当前工作区会话 ID。
    def _session_id_from_reference(self, reference: str) -> str:
        normalized = reference.strip()
        if normalized in self._sessions:
            return normalized
        candidate = Path(normalized).expanduser()
        for session_id in self._sessions:
            directory = self._store.session_dir(session_id)
            if candidate == directory or candidate == directory / "thread.jsonl":
                return session_id
            try:
                resolved_targets = {
                    directory.resolve(),
                    (directory / "thread.jsonl").resolve(),
                }
                if candidate.resolve() in resolved_targets:
                    return session_id
            except OSError:
                continue
        raise HandlerError(INVALID_PARAMS, "session reference does not exist")

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
        host = await self.prepare_extensions(sid)
        if host is not None:
            await host.emit_session_event({"type": "session_start", "reason": "resume"})
            await self._discover_extension_resources(sid, host, reason="startup")
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
        host = self._extension_hosts.get(sid)
        if host is not None:
            await host.emit_session_event({
                "type": "session_info_changed", "name": normalized,
            })
        return session

    # 读取会话分支树供 TUI 和 Web 选择历史继续点。
    async def tree(self, sid: str) -> list[dict[str, Any]]:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        return self._store.session_tree(sid)

    # 切换同一会话的上下文路径，不执行任务也不回滚工作区文件。
    async def navigate_tree(
        self, sid: str, target_seq: int, *, summarize: bool = False, focus: str = "",
        label: str = "",
    ) -> dict[str, Any]:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "wait for the active turn before navigating history")
        tree = self._store.session_tree(sid)
        parents = {int(entry["seq"]): entry["parent_seq"] for entry in tree}
        old_leaf = next((int(entry["seq"]) for entry in reversed(tree) if entry["active"]), None)
        old_active = {int(entry["seq"]) for entry in tree if entry["active"]}
        cursor: int | None = target_seq
        common_ancestor = None
        while cursor is not None:
            if cursor in old_active:
                common_ancestor = cursor
                break
            cursor = parents.get(cursor)
        branch_entries = self._store.branch_entries(sid, target_seq)
        host = await self.prepare_extensions(sid)
        decision = None
        if host is not None:
            decision = await host.emit_session_event({
                "type": "session_before_tree",
                "preparation": {
                    "targetId": str(target_seq),
                    "oldLeafId": str(old_leaf) if old_leaf is not None else None,
                    "commonAncestorId": (
                        str(common_ancestor) if common_ancestor is not None else None
                    ),
                    "entriesToSummarize": [row for _, row in branch_entries],
                    "userWantsSummary": summarize,
                    "customInstructions": focus or None,
                    "replaceInstructions": False,
                    "label": label or None,
                },
            })
            if decision is not None and decision.get("cancel") is True:
                raise HandlerError(INVALID_PARAMS, "session tree navigation cancelled by extension")
        resolved_focus = (
            str(decision["customInstructions"])
            if decision is not None and isinstance(decision.get("customInstructions"), str)
            else focus
        )
        replace_instructions = bool(decision and decision.get("replaceInstructions") is True)
        resolved_label = (
            str(decision["label"])
            if decision is not None and isinstance(decision.get("label"), str)
            else label
        )
        extension_summary = decision.get("summary") if decision is not None else None
        supplied_summary = (
            str(extension_summary.get("summary", ""))
            if isinstance(extension_summary, dict) else ""
        )
        async with lock:
            summary = supplied_summary
            if summarize and not supplied_summary:
                from code_rook.core.agent_runtime.branch_summary import summarize_branch
                from code_rook.core.llm.factory import create_provider_for_resolved_route

                provider = self._provider
                context_window = 128_000
                if self._route_registry is not None or (host is not None and session.route_id):
                    route = self._resolve_model_route(
                        session.route_id or None,
                        session.model or None,
                        host,
                    )
                    route = self._apply_session_thinking(session, route)
                    provider = create_provider_for_resolved_route(route)
                    context_window = route.route.context_window or context_window
                if provider is None:
                    raise HandlerError(-32020, "provider not available for branch summary")
                summary_bus = EventBus()
                operation_id = f"branch-summary-{uuid.uuid4().hex}"

                # 分支摘要不创建编码 Turn，用量独立入账并投影到会话事件。
                async def record_summary_usage(event: BaseModel) -> None:
                    if getattr(event, "type", "") != "llm.usage":
                        return
                    payload = event.model_dump(mode="json")
                    payload["operation_id"] = operation_id
                    payload["purpose"] = "branch_summary"
                    recorded = self._store.append_session_event(
                        sid, event_type="session.auxiliary_usage", payload=payload,
                    )
                    if self._runtime is not None:
                        await self._runtime.record_auxiliary_usage(sid, payload, recorded.seq)

                summary_bus.subscribe(record_summary_usage, critical=True)

                # 摘要请求及尝试独立入账，不伪装成一次编码任务
                def audit_summary(event_type: str, payload: dict[str, Any]) -> None:
                    self._store.append_session_event(sid, event_type=event_type, payload=payload)

                summary = await summarize_branch(
                    branch_entries, provider, focus=resolved_focus,
                    replace_instructions=replace_instructions,
                    context_window=context_window,
                    reserve_tokens=self._compaction_config.reserve_tokens,
                    retry_policy=self._summary_retry_policy,
                    bus=summary_bus, run_id=operation_id,
                    audit=audit_summary,
                )
            result = self._store.navigate_tree(
                sid, target_seq, summary=summary, label=resolved_label,
            )
            self._pending_plans.pop(sid, None)
            self._pending_plans_loaded.add(sid)
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
                await self._runtime.record_navigation(sid, target_seq, result["ledger_seq"])
        if host is not None:
            summary_entry = (
                {"summary": summary, "label": resolved_label or None} if summary else None
            )
            await host.emit_session_event({
                "type": "session_tree",
                "newLeafId": str(result["leaf_seq"]) if result["leaf_seq"] else None,
                "oldLeafId": str(old_leaf) if old_leaf is not None else None,
                "summaryEntry": summary_entry,
                "fromExtension": bool(supplied_summary),
            })
        return {"session_id": sid, **result, "messages": self._store.read_messages(sid)}

    # 从现有会话创建历史副本，并允许仅在新 fork 上冻结不同 Preset
    async def fork(
        self,
        sid: str,
        title: str = "",
        *,
        preset_id: str | None = None,
        leaf_seq: int | None = None,
        position: str = "at",
    ) -> Session:
        await self._ensure_runtime_sessions()
        source = self._get_session(sid)
        source_lock = self._locks[sid]
        if source_lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        source_host = await self.prepare_extensions(sid)
        if source_host is not None:
            decision = await source_host.emit_session_event({
                "type": "session_before_fork",
                "entryId": str(leaf_seq) if leaf_seq is not None else "",
                "position": position,
            })
            if decision is not None and decision.get("cancel") is True:
                raise HandlerError(INVALID_PARAMS, "session fork cancelled by extension")

        async with source_lock:
            if leaf_seq is not None:
                from code_rook.core.compact.protocol import validate_tool_protocol

                messages = self._store.derive_messages(sid, leaf_seq=leaf_seq, trim_orphans=False)
                valid, errors = validate_tool_protocol(messages)
                if not valid:
                    raise ValueError("select a complete tool result: " + "; ".join(errors))
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
                route_id=source.route_id,
                model=source.model,
                thinking_level=source.thinking_level,
            )
            self._store.create_fork(source.id, forked)
            if leaf_seq is not None:
                self._store.select_branch(fork_id, leaf_seq)
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
        fork_host = await self.prepare_extensions(forked.id)
        if fork_host is not None:
            await fork_host.emit_session_event({
                "type": "session_start", "reason": "fork",
                "previousSessionFile": str(self._store.session_dir(source.id) / "thread.jsonl"),
            })
            await self._discover_extension_resources(
                forked.id, fork_host, reason="startup"
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
                self._store.read_messages(sid)
                if export_format == "json"
                else self._store.derive_messages(sid, display=True),
                self._store.read_notes(sid),
                export_format,
            )

    # 将 CodeRook JSON 或 Pi JSONL 转成当前工作区内可继续的原生会话。
    async def import_session(
        self,
        content: str,
        *,
        filename: str = "",
        title: str = "",
    ) -> tuple[Session, int, SessionImportFormat]:
        imported = import_session_content(content)
        resolved_title = title.strip() or imported.title.strip() or "Imported session"
        session = await self.create("chat", resolved_title[:200])
        async with self._locks[session.id]:
            for index, message in enumerate(imported.messages):
                self._store.append_message(
                    session.id,
                    str(message["role"]),
                    message["content"],
                    run_id="session-import",
                    message_id=f"session-import:{index}",
                )
            if imported.notes:
                self._store.write_notes(session.id, imported.notes)
            self._store.append_session_event(
                session.id,
                event_type="session.imported",
                payload={
                    "source_format": imported.source_format,
                    "filename": Path(filename).name if filename else "",
                    "message_count": len(imported.messages),
                },
                provenance="import",
            )
            session.status = "waiting_for_input"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
        await self._bus.publish(
            SessionWaitingForInputEvent(
                session_id=session.id,
                last_run_id="",
                ts=session.updated_at,
            )
        )
        return session, len(imported.messages), imported.source_format

    async def delete(self, sid: str) -> None:
        await self._ensure_runtime_sessions()
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        host = self._extension_hosts.get(sid)
        if host is not None:
            await host.emit_session_event({"type": "session_shutdown", "reason": "quit"})
        async with lock:
            if self._hooks is not None:
                await self._hooks.emit(
                    "session_stop",
                    {"session_id": sid, "reason": "deleted"},
                )
            self._store.delete_session(sid)
            host = self._extension_hosts.pop(sid, None)
            self._clear_extension_resources(sid)
            if host is not None:
                await host.close()
        queue_task = self._queue_dispatch_tasks.pop(sid, None)
        self._queue_wakeups.pop(sid, None)
        if queue_task is not None and not queue_task.done():
            queue_task.cancel()
            await asyncio.gather(queue_task, return_exceptions=True)
        self._sessions.pop(sid, None)
        self._locks.pop(sid, None)
        self._pending_plans.pop(sid, None)
        self._pending_plans_loaded.discard(sid)
        self._pending_extension_messages.pop(sid, None)
        self._next_turn_extension_messages.pop(sid, None)
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

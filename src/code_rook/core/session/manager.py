from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from code_rook.core.artifacts import (
    ArtifactError,
    ArtifactStore,
    ImageArtifactInput,
    inspect_image,
)
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.envelope import INVALID_PARAMS, HandlerError
from code_rook.core.bus.events import (
    PlanReadyEvent,
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
from code_rook.core.checkpoints import CheckpointError, CheckpointStore
from code_rook.core.compact.protocol import estimate_messages_tokens
from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.route_registry import RouteResolutionError
from code_rook.core.memory import MemoryStore
from code_rook.core.runs import new_run_id
from code_rook.core.runtime.models import TurnStatus
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.session.exporter import SessionExportFormat, export_session
from code_rook.core.session.model import Session, SessionMode
from code_rook.core.session.store import SessionStore
from code_rook.core.skills.loader import SkillError, SkillLoader
from code_rook.core.skills.models import Skill
from code_rook.core.task.model import Task
from code_rook.core.workspace import WorkspaceBoundary

if TYPE_CHECKING:
    from code_rook.core.goal import GoalRecord, GoalService
    from code_rook.core.llm.base import LLMProvider
    from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
    from code_rook.core.runner import AgentRunner
    from code_rook.core.subagent.registry import BackgroundTaskRegistry

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012
SESSION_NOT_RESUMABLE = -32013
RUN_NOT_ACTIVE = -32014


@dataclass
class _ActiveRun:
    session_id: str
    task: asyncio.Task[Any]
    finished: asyncio.Event


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
    ) -> None:
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
        self._runtime_bootstrapped = False
        self._runtime_bootstrap_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._active_runs: dict[str, _ActiveRun] = {}
        self._skill_loader = SkillLoader()
        workspace = WorkspaceBoundary.current().root
        self._workspace = workspace.resolve()
        self._artifact_store = ArtifactStore(workspace / ".coderook" / "artifacts")
        self._rehydrate()

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

    # 从磁盘恢复会话索引；active 表示 daemon 在一次 run 中退出，恢复为 interrupted
    def _rehydrate(self) -> None:
        for session in self._store.list_sessions():
            if not session.workspace:
                session.workspace = str(self._workspace)
                self._store.write_meta(session)
            if Path(session.workspace).resolve() != self._workspace:
                continue
            if session.status == "active":
                self._store.recover_incomplete_tail(session.id)
                session.status = "interrupted"
                self._store.write_meta(session)
            self._sessions[session.id] = session
            self._locks[session.id] = asyncio.Lock()

    # 创建新 session 并写入 meta.json
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        await self._ensure_runtime_sessions()
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
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        if self._runtime is not None:
            await self._runtime.sync_session(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(
        self,
        sid: str,
        content: str,
        *,
        run_id: str | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
        attachments: list[ImageArtifactInput] | None = None,
    ) -> str:
        await self._ensure_runtime_sessions()
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            resolved_route: ResolvedRoute | None = None
            if self._route_registry is not None:
                try:
                    resolved_route = self._route_registry.resolve()
                except RouteResolutionError as exc:
                    raise HandlerError(INVALID_PARAMS, str(exc)) from exc
            image_attachments = attachments or []
            if (
                image_attachments
                and resolved_route is not None
                and not resolved_route.route.supports_images
            ):
                raise HandlerError(
                    INVALID_PARAMS,
                    "active route does not support images; select an image-capable route",
                )
            attachment_text, image_blocks = await self._prepare_image_attachments(
                image_attachments
            )
            ledger_content = (
                f"{content.rstrip()}\n\n{attachment_text}".strip()
                if attachment_text
                else content
            )

            if session.status in ("waiting_for_input", "interrupted"):
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            run_id = run_id or new_run_id()
            requested_skill: Skill | None = None
            skill_name = ""
            skill_arguments = ""
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                skill_arguments = parts[1] if len(parts) > 1 else ""
                try:
                    requested_skill = self._skill_loader.resolve(skill_name)
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
                session.title = content[:40]

            session.run_ids.append(run_id)
            session.status = "active"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.start_turn(
                    session,
                    run_id,
                    ledger_content,
                    runtime_mode=runtime_mode,
                    route=(resolved_route.receipt if resolved_route is not None else None),
                )

            # Skill 解析：检测 "/" 前缀，展开为系统提示覆盖和工具白名单
            goal = ledger_content
            system_prompt_override: str | None = None
            tool_whitelist: list[str] | None = None
            if requested_skill is not None:
                goal = self._skill_loader.render_prompt(requested_skill, skill_arguments)
                system_prompt_override = requested_skill.system_prompt_template
                tool_whitelist = requested_skill.allowed_tools or None
                await self._bus.publish(
                    SkillInvokedEvent(
                        skill_name=skill_name,
                        arguments=skill_arguments,
                        run_id=run_id,
                        ts=_now(),
                    )
                )

            runner = self._runner_factory()
            active_goal: GoalRecord | None = None
            persistent_goal_context = ""
            if self._goal_service is not None:
                candidate = self._goal_service.current(sid)
                if candidate is not None and candidate.status == "active":
                    active_goal = self._goal_service.start_run(candidate.id, run_id)
                    persistent_goal_context = self._goal_service.render_context(active_goal)
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
            if image_blocks:
                run_options["initial_images"] = image_blocks
            run_coroutine = runner.run_and_capture(goal, **run_options)
            runner_task = asyncio.create_task(
                run_coroutine,
                name=f"run:{run_id}",
            )
            active = _ActiveRun(
                session_id=sid,
                task=runner_task,
                finished=asyncio.Event(),
            )
            self._active_runs[run_id] = active
            try:
                outcome = await runner_task
            except asyncio.CancelledError:
                if active_goal is not None and self._goal_service is not None:
                    self._goal_service.finish_run(
                        active_goal.id,
                        run_id,
                        succeeded=False,
                        reason="cancelled",
                    )
                session.status = "interrupted"
                session.updated_at = _now()
                self._store.write_meta(session)
                if self._runtime is not None:
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
            finally:
                self._active_runs.pop(run_id, None)
                active.finished.set()

            session.updated_at = _now()
            if active_goal is not None and self._goal_service is not None:
                self._goal_service.finish_run(
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
                await self._bus.publish(
                    PlanReadyEvent(
                        session_id=sid,
                        run_id=run_id,
                        request=content,
                        plan=outcome.result.strip(),
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
            return run_id

    # 返回指定会话当前是否正持有 turn 执行锁
    def is_busy(self, sid: str) -> bool:
        self._get_session(sid)
        return self._locks[sid].locked()

    # 返回指定 session 的 active run ID，不存在时返回 None
    def active_run_id(self, sid: str) -> str | None:
        self._get_session(sid)
        for run_id, active in self._active_runs.items():
            if active.session_id == sid and not active.task.done():
                return run_id
        return None

    # 返回当前 workspace 正在执行的 run 数，供启动器安全切换 daemon 工作目录
    def active_run_count(self) -> int:
        return sum(not active.task.done() for active in self._active_runs.values())

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

    # 安全恢复指定（默认最近一次）run 中用户明确选择的 checkpoint
    def rewind(
        self, sid: str, checkpoint_id: str, run_id: str | None = None
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
                checkpoint_id
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
            session.status = "waiting_for_input"
            session.updated_at = _now()
            self._store.write_meta(session)
            if self._runtime is not None:
                await self._runtime.sync_session(session)
            await self._bus.publish(SessionResumedEvent(session_id=sid, ts=session.updated_at))
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

    async def fork(self, sid: str, title: str = "") -> Session:
        await self._ensure_runtime_sessions()
        source = self._get_session(sid)
        source_lock = self._locks[sid]
        if source_lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with source_lock:
            fork_id = f"sess-{uuid.uuid4().hex[:12]}"
            ts = _now()
            fork_title = title.strip() or f"{source.title or source.id} (fork)"
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
            )
            self._store.create_fork(source.id, forked)
            self._sessions[fork_id] = forked
            self._locks[fork_id] = asyncio.Lock()
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
        self._sessions.pop(sid, None)
        self._locks.pop(sid, None)
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

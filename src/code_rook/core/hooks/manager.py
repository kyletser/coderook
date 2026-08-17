from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from code_rook.core.events.bus import EventBus
from code_rook.core.hooks.config import load_hook_configs
from code_rook.core.hooks.models import (
    HookAuditEvent,
    HookAuditStatus,
    HookConfig,
    HookEvent,
)
from code_rook.core.hooks.payload import build_hook_payload
from code_rook.core.hooks.process import HookProcessResult, execute_hook_process

LegacyHookEvent = Literal["UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
HookCallback = Callable[[dict[str, Any]], Awaitable["HookDecision | None"]]
ProjectTrustProvider = Callable[[str], bool]

_EVENT_ALIASES: dict[LegacyHookEvent, HookEvent] = {
    "UserPromptSubmit": "message_submit",
    "PreToolUse": "tool_call_before",
    "PostToolUse": "tool_call_after",
    "Stop": "turn_stop",
}
_ALL_EVENTS: tuple[HookEvent, ...] = (
    "session_start",
    "message_submit",
    "turn_start",
    "tool_call_before",
    "tool_call_after",
    "approval_requested",
    "compaction_completed",
    "worker_started",
    "worker_finished",
    "turn_stop",
    "session_stop",
)

logger = logging.getLogger(__name__)


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class HookDecision:
    blocked: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _QueuedHook:
    config: HookConfig
    event: HookEvent
    context: dict[str, Any]


# 将旧生命周期名称归一化到 Hooks V2 事件名
def _normalize_event(event: HookEvent | LegacyHookEvent) -> HookEvent:
    return _EVENT_ALIASES.get(cast(LegacyHookEvent, event), cast(HookEvent, event))


# 按点分路径从 hook context 提取条件值
def _condition_value(context: dict[str, Any], path: str) -> Any:
    current: Any = context
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


# 使用大小写敏感 glob 匹配全部声明条件
def _conditions_match(config: HookConfig, context: dict[str, Any]) -> bool:
    for path, pattern in config.conditions.items():
        value = _condition_value(context, path)
        values = value if isinstance(value, list | tuple | set | frozenset) else [value]
        if not any(fnmatch.fnmatchcase(str(item), pattern) for item in values):
            return False
    return True


class HookManager:
    # 初始化回调兼容层、进程 hooks、有界非阻断队列和结构化审计缓冲
    def __init__(
        self,
        configs: list[HookConfig] | None = None,
        *,
        workspace: Path | None = None,
        bus: EventBus | None = None,
        queue_size: int = 64,
        project_trust_provider: ProjectTrustProvider | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("hook queue_size must be positive")
        self._workspace = (workspace or Path.cwd()).resolve()
        self._configs = tuple(configs or [])
        self._callbacks: dict[HookEvent, list[HookCallback]] = {
            event: [] for event in _ALL_EVENTS
        }
        self._bus = bus
        self._queue: asyncio.Queue[_QueuedHook] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._project_trust_provider = project_trust_provider
        self._audit: deque[HookAuditEvent] = deque(maxlen=1000)

    @classmethod
    # 从用户和项目 hooks.toml 构造 V2 manager，项目配置仍在运行时接受 trust 检查
    def from_workspace(
        cls,
        workspace: Path,
        *,
        user_config: Path | None = None,
        bus: EventBus | None = None,
        queue_size: int = 64,
        project_trust_provider: ProjectTrustProvider | None = None,
    ) -> HookManager:
        return cls(
            load_hook_configs(workspace, user_config=user_config),
            workspace=workspace,
            bus=bus,
            queue_size=queue_size,
            project_trust_provider=project_trust_provider,
        )

    # 注册一个内存异步 hook，并保持注册顺序执行
    def register(
        self,
        event: HookEvent | LegacyHookEvent,
        callback: HookCallback,
    ) -> None:
        self._callbacks[_normalize_event(event)].append(callback)

    # 以只读 tuple 形式公开当前已加载的 hook 配置，供管理面板列出
    @property
    def configs(self) -> tuple[HookConfig, ...]:
        return self._configs

    # 返回当前进程内最近的结构化 hook 审计事件快照
    def audit_events(self) -> tuple[HookAuditEvent, ...]:
        return tuple(self._audit)

    # 手动重跑指定 hook_id 的进程 hook，返回本次审计记录（未找到返回 None）
    async def rerun(self, hook_id: str) -> HookAuditEvent | None:
        config = next((item for item in self._configs if item.id == hook_id), None)
        if config is None:
            return None
        await self._execute(config, config.event, {"session_id": "", "rerun": True})
        return self._audit[-1] if self._audit else None

    # 触发生命周期事件，阻断 hook 同步决策，非阻断 hook 进入有界队列
    async def emit(
        self,
        event: HookEvent | LegacyHookEvent,
        context: dict[str, Any],
    ) -> HookDecision:
        normalized = _normalize_event(event)
        for callback in tuple(self._callbacks[normalized]):
            try:
                decision = await callback(context)
            except Exception:
                logger.exception("hook callback failed event=%s callback=%r", normalized, callback)
                continue
            if decision is not None and decision.blocked:
                return decision

        for config in self._configs:
            if config.event != normalized or not _conditions_match(config, context):
                continue
            if not self._is_trusted(config, context):
                await self._record_audit(
                    config,
                    status="skipped_untrusted",
                    elapsed_ms=0,
                    reason="project hook skipped in an untrusted workspace",
                )
                continue
            if not config.blocking:
                self._ensure_worker()
                try:
                    self._queue.put_nowait(_QueuedHook(config, normalized, dict(context)))
                except asyncio.QueueFull:
                    await self._record_audit(
                        config,
                        status="dropped",
                        elapsed_ms=0,
                        reason="non-blocking hook queue is full",
                    )
                continue
            decision = await self._execute(config, normalized, context)
            if decision.blocked:
                return decision
        return HookDecision()

    # 等待已排队 hook 完成并停止后台 worker
    async def close(self) -> None:
        if self._worker is None:
            return
        await self._queue.join()
        self._worker.cancel()
        await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None

    # 判断项目级 hook 是否获得当前 session 的显式可信工作区授权
    def _is_trusted(self, config: HookConfig, context: dict[str, Any]) -> bool:
        if config.trusted_scope != "project":
            return True
        if self._project_trust_provider is None:
            return False
        return self._project_trust_provider(str(context.get("session_id", "")))

    # 在首次非阻断投递时懒启动单一队列 worker
    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_queue(), name="hook-nonblocking-worker")

    # 串行消费有界队列，确保非阻断 hook 不产生无界进程并发
    async def _run_queue(self) -> None:
        while True:
            queued = await self._queue.get()
            try:
                await self._execute(queued.config, queued.event, queued.context)
            except Exception:
                logger.exception("non-blocking hook failed hook_id=%s", queued.config.id)
            finally:
                self._queue.task_done()

    # 执行进程 hook，解析阻断结果并应用 fail-open/fail-closed 策略
    async def _execute(
        self,
        config: HookConfig,
        event: HookEvent,
        context: dict[str, Any],
    ) -> HookDecision:
        started = time.monotonic()
        payload = build_hook_payload(config, event, context)
        cwd = self._workspace if config.trusted_scope == "project" else Path.home()
        try:
            result = await execute_hook_process(config, payload, cwd=cwd)
        except Exception as exc:
            logger.exception("hook process failed hook_id=%s", config.id)
            result = HookProcessResult("failed", None, "", str(exc), False)
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        blocked, reason = self._decision_from_result(config, result)
        audit_status: HookAuditStatus = cast(HookAuditStatus, result.status)
        if blocked and result.status == "completed":
            audit_status = "blocked"
        await self._record_audit(
            config,
            status=audit_status,
            elapsed_ms=elapsed_ms,
            blocked=blocked,
            reason=reason,
            output_truncated=result.output_truncated,
            exit_code=result.exit_code,
        )
        return HookDecision(blocked=blocked, reason=reason)

    # 从 hook stdout JSON 和 failure policy 计算最终阻断决定
    def _decision_from_result(
        self,
        config: HookConfig,
        result: HookProcessResult,
    ) -> tuple[bool, str]:
        if result.status != "completed":
            reason = result.stderr.strip() or f"hook {config.id} {result.status}"
            return config.on_failure == "closed", reason
        try:
            response = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            response = {}
        if not isinstance(response, dict):
            response = {}
        blocked = bool(response.get("blocked", False))
        reason = str(response.get("reason", ""))
        return blocked, reason

    # 保存结构化审计并通过 EventBus 暴露给 transcript、trace 和 TUI
    async def _record_audit(
        self,
        config: HookConfig,
        *,
        status: HookAuditStatus,
        elapsed_ms: int,
        blocked: bool = False,
        reason: str = "",
        output_truncated: bool = False,
        exit_code: int | None = None,
    ) -> None:
        audit = HookAuditEvent(
            hook_id=config.id,
            event=config.event,
            status=status,
            blocking=config.blocking,
            on_failure=config.on_failure,
            elapsed_ms=elapsed_ms,
            blocked=blocked,
            reason=reason[:1000],
            output_truncated=output_truncated,
            exit_code=exit_code,
            ts=_now(),
        )
        self._audit.append(audit)
        if self._bus is not None:
            from code_rook.core.bus.events import HookExecutedEvent

            await self._bus.publish(
                HookExecutedEvent(
                    hook_id=audit.hook_id,
                    event_name=audit.event,
                    status=audit.status,
                    blocking=audit.blocking,
                    on_failure=audit.on_failure,
                    elapsed_ms=audit.elapsed_ms,
                    blocked=audit.blocked,
                    reason=audit.reason,
                    output_truncated=audit.output_truncated,
                    exit_code=audit.exit_code,
                    ts=audit.ts,
                )
            )

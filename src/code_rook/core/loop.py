from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from code_rook.core.agent_runtime.messages import prepare_model_messages
from code_rook.core.agent_runtime.prompt import build_system_prompt
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.events import (
    AgentRepeatNoticeEvent,
    ContextBudgetEvent,
    ContextPrefixFingerprintEvent,
    ContextWorkingSetEvent,
    LlmAttemptFinishedEvent,
    LlmRequestPreparedEvent,
    LlmRetryEvent,
    LspDiagnosticsEvent,
    StepFinishedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    VerificationCompletedEvent,
    VerificationFailedEvent,
)
from code_rook.core.compact.budget import truncate_tool_results
from code_rook.core.compact.protocol import estimate_messages_tokens
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.execution.invariants import (
    InvariantViolation,
    validate_request_snapshot,
)
from code_rook.core.execution.models import RequestSnapshot
from code_rook.core.llm.attempt import AttemptBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.errors import ProviderRequestError
from code_rook.core.llm.retry import RetryPolicy
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.lsp import WorkspaceDiagnosticsClient
from code_rook.core.prefix_fingerprint import PrefixFingerprintTracker
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.invocation import PreparedToolArguments, invoke_tool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ParallelPolicy, ResourceClaim, ToolCatalogError
from code_rook.core.turn import (
    NoContentResponseError,
    ReadRepeatGuard,
    StreamIdleTimeoutError,
    StreamWallTimeoutError,
    StreamWatchdog,
    StuckGuard,
)
from code_rook.core.working_set import WorkingSetSource

if TYPE_CHECKING:
    from code_rook.core.artifacts import ArtifactStore
    from code_rook.core.authority import AuthoritySnapshot
    from code_rook.core.compact.compactor import Compactor
    from code_rook.core.hooks import HookManager
    from code_rook.core.interaction import InteractionManager
    from code_rook.core.permissions.manager import PermissionManager
    from code_rook.core.task.manager import TodoStateView


log = logging.getLogger(__name__)


class RouteCapabilityError(RuntimeError):
    # 创建冻结路由能力不匹配错误，阻止静默降级执行
    def __init__(self, capability: str) -> None:
        super().__init__(f"selected route does not support {capability}")
        self.capability = capability

_CONTEXT_ERROR_MARKERS = (
    "context_length_exceeded",
    "max_context_window",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
)
_TRANSIENT_ERROR_MARKERS = ("429", "529", "rate limit", "overloaded", "temporarily unavailable")


# 把不可信工具结果字段安全转换为非负整数，非法值使用调用方默认值
def _nonnegative_int(value: object, *, default: int = 0) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        return max(0, default)
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return max(0, default)


# 交互模式经结构化提问最多允许的步数续段次数，防止无限续跑
_MAX_ASK_STEP_CONTINUES = 3
_CONTENT_HASH_RE = re.compile(
    r"(?:content_hash[=:]\s*|\"(?:new_hash|content_hash)\"\s*:\s*\")([^\s,\")]+)"
)


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class TranscriptSink(Protocol):
    def append_assistant(self, step: int, blocks: list[dict[str, object]]) -> None: ...

    def append_user(self, step: int, content: str | list[dict[str, Any]]) -> None: ...

    def append_custom(
        self,
        step: int,
        *,
        custom_type: str,
        content: str | list[dict[str, Any]],
        display: bool,
        details: object = None,
    ) -> int: ...

    def append_tool_result(
        self,
        step: int,
        tool_use_id: str,
        content: str | list[dict[str, Any]],
        *,
        is_error: bool,
        block_index: int,
        block_count: int,
    ) -> None: ...

    def append_request_snapshot(self, step: int, snapshot: RequestSnapshot) -> int | None: ...

    def latest_request_snapshot(self) -> RequestSnapshot | None: ...

    # 记录不参与模型消息投影的尝试和重试事实
    def append_audit(self, step: int, event_type: str, payload: dict[str, object]) -> int: ...

    # 将插件提醒附在完整工具结果后并保留来源
    def append_notice(self, step: int, content: str) -> None: ...


class AgentLoop:
    # 初始化循环依赖，以及可选的权限管理器、压缩器和 session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        compact_threshold: float = 0.80,
        session_id: str = "",
        transcript: TranscriptSink | None = None,
        hooks: HookManager | None = None,
        tool_result_limit: int = 8_000,
        tool_result_keep: int = 4_000,
        tool_result_summarize_threshold: int = 20_000,
        todo_state: TodoStateView | None = None,
        interaction_manager: InteractionManager | None = None,
        artifact_store: ArtifactStore | None = None,
        watchdog: StreamWatchdog | None = None,
        stuck_guard: StuckGuard | None = None,
        read_guard: ReadRepeatGuard | None = None,
        retry_backoff_s: float = 0.5,
        retry_policy: RetryPolicy | None = None,
        diagnostics_client: WorkspaceDiagnosticsClient | None = None,
        prefix_tracker: PrefixFingerprintTracker | None = None,
        escalate_plan_thinking: bool = False,
        supports_tools: bool = True,
        supports_parallel_tools: bool = True,
        supports_images: bool = True,
        auto_step_continues: int = 0,
        authority_snapshot: AuthoritySnapshot | None = None,
        request_metadata: dict[str, object] | None = None,
        transform_context: Callable[
            [list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]
        ] | None = None,
        transform_provider_request: Callable[
            [dict[str, Any]], Awaitable[dict[str, Any]]
        ] | None = None,
        provider_response_sink: Callable[[LlmResponse], Awaitable[None]] | None = None,
        agent_event_sink: Callable[
            [dict[str, Any]], Awaitable[dict[str, Any] | None]
        ] | None = None,
        # 可选的每步路由刷新回调：返回新 provider 表示需切换（loop 接管重建后的实例），
        # 返回 None 表示沿用当前 provider；用于 per-turn 模型切换（W2.4）
        route_refresher: Callable[[int], Awaitable[LLMProvider | None]] | None = None,
    ) -> None:
        if retry_backoff_s < 0:
            raise ValueError("retry_backoff_s must not be negative")
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._compact_threshold = compact_threshold
        self._session_id = session_id
        self._transcript = transcript
        self._hooks = hooks
        self._tool_result_limit = tool_result_limit
        self._tool_result_keep = tool_result_keep
        self._tool_result_summarize_threshold = tool_result_summarize_threshold
        self._todo_state = todo_state
        self._interaction_manager = interaction_manager
        self._artifact_store = artifact_store
        self._watchdog = watchdog or StreamWatchdog()
        self._stuck_guard = stuck_guard or StuckGuard()
        self._read_guard = read_guard or ReadRepeatGuard()
        self._retry_policy = retry_policy or RetryPolicy(initial_delay_s=retry_backoff_s)
        self._pending_repeat_notices: list[tuple[AgentRepeatNoticeEvent, str]] = []
        self._diagnostics_client = diagnostics_client
        self._prefix_tracker = prefix_tracker or PrefixFingerprintTracker()
        self._escalate_plan_thinking = escalate_plan_thinking
        self._supports_tools = supports_tools
        self._supports_parallel_tools = supports_parallel_tools
        self._supports_images = supports_images
        self._auto_step_continues = max(0, auto_step_continues)
        self._authority_snapshot = authority_snapshot
        self._request_metadata = dict(request_metadata or {})
        self._transform_context = transform_context
        self._transform_provider_request = transform_provider_request
        self._provider_response_sink = provider_response_sink
        self._agent_event_sink = agent_event_sink
        self._route_refresher = route_refresher
        self._step_continues_used = 0
        self._initial_max_steps: int | None = None
        self._reactive_compaction_attempted = False
        self._length_continues_used = 0
        self._active_step = 0
        # 防 end_turn 早退 reminder 防抖：跟踪 todos 摘要快照与已提醒次数

    # 判断异常是否表示上下文窗口已超限
    @staticmethod
    def _is_context_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _CONTEXT_ERROR_MARKERS)

    # 判断异常是否适合短暂退避后重试
    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        if isinstance(exc, ProviderRequestError):
            return exc.retryable
        if isinstance(exc, (StreamIdleTimeoutError, StreamWallTimeoutError)):
            return True
        message = f"{type(exc).__name__} {exc}".lower()
        return any(marker in message for marker in _TRANSIENT_ERROR_MARKERS)

    # 判断 provider 是否返回了既无正文、推理也无工具调用的空响应
    @staticmethod
    def _is_no_content(response: LlmResponse) -> bool:
        if response.completion_status in {
            "length",
            "incomplete",
            "content_filtered",
            "failed",
            "cancelled",
            "transport_error",
        }:
            return False
        return not (
            response.text.strip()
            or response.tool_calls
            or response.thinking_blocks
        )

    # 显式让出一次事件循环，使已请求的任务取消在启动下一操作前生效
    @staticmethod
    async def _cancellation_checkpoint() -> None:
        await asyncio.sleep(0)

    # 预测下一请求的上下文占比并为模型输出预留固定安全空间
    def _projected_context_pct(
        self,
        context: ExecutionContext,
        response: LlmResponse,
        *,
        output_reserve_tokens: int = 4_096,
        usage_message_count: int | None = None,
    ) -> float:
        usage = response.usage
        if usage is None:
            return 0.0
        # Anthropic 的 input_tokens 不含缓存 token 而 context_pct 含；容量推算的分子
        # 必须与 context_pct 同口径，否则会把窗口低估、投影占比虚高
        context_tokens = (
            usage.input_tokens
            + usage.cache_read_input_tokens
            + usage.cache_creation_input_tokens
        )
        window = getattr(self._provider, "context_window", None)
        if isinstance(window, int) and window > 0:
            estimated_capacity = float(window)
        elif context_tokens > 0 and usage.context_pct > 0:
            estimated_capacity = context_tokens / usage.context_pct
        else:
            return 0.0
        if usage_message_count is None:
            projected_input = estimate_messages_tokens(context.messages) + output_reserve_tokens
        else:
            trailing = context.messages[usage_message_count:]
            projected_input = (
                context_tokens + usage.output_tokens
                + (estimate_messages_tokens(trailing) if trailing else 0)
                + output_reserve_tokens
            )
        return projected_input / max(1.0, estimated_capacity)

    # 在 watchdog 下执行同一步请求，所有临时错误共享显式重试策略
    async def _call_provider(self, context: ExecutionContext) -> LlmResponse:
        retries = 0
        system_prompt = self._render_system(context)
        tool_schemas = self._registry.tool_schemas() if self._supports_tools else []
        await self._bus.publish(
            ContextBudgetEvent(
                run_id=context.run_id,
                step=context.step,
                message_tokens=estimate_messages_tokens(context.messages),
                system_tokens=max(1, len(system_prompt) // 4),
                tool_schema_tokens=max(1, len(json.dumps(tool_schemas, sort_keys=True)) // 4),
                tool_count=len(tool_schemas),
                ts=_now(),
            )
        )
        prefix = self._prefix_tracker.observe(
            system_prompt=context.stable_system_prompt(
                build_system_prompt(self._registry.prompt_tools(tool_schemas)),
            ),
            tool_catalog=json.dumps(
                tool_schemas,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            stable_memory=context.stable_memory_text(),
        )
        await self._bus.publish(
            ContextPrefixFingerprintEvent(
                run_id=context.run_id,
                step=context.step,
                digest=prefix.digest,
                source_hashes=prefix.source_hashes,
                changed_sources=list(prefix.changed_sources),
                ts=_now(),
            )
        )

        flushed_images = self._flush_pending_images(context)
        thinking_override = self._thinking_override_for(context)
        request_messages = context.messages
        if self._transform_context is not None:
            request_messages = await self._transform_context(deepcopy(request_messages))
        prepared_messages = prepare_model_messages(
            request_messages, supports_images=self._supports_images,
            wire_format=str(self._request_metadata.get("wire_format", "")),
            model=str(self._request_metadata.get("model", "")),
            route_id=str(self._request_metadata.get("route_id", "")),
        )
        request_payload: dict[str, Any] = {
            "messages": prepared_messages,
            "system": system_prompt,
            "tool_schemas": tool_schemas,
        }
        if self._transform_provider_request is not None:
            request_payload = await self._transform_provider_request(
                deepcopy(request_payload)
            )
        raw_messages = request_payload.get("messages")
        raw_system = request_payload.get("system")
        raw_tools = request_payload.get("tool_schemas")
        if not isinstance(raw_messages, list) or any(
            not isinstance(message, dict) for message in raw_messages
        ):
            raise InvariantViolation("provider request hook returned invalid messages")
        if raw_system is not None and not isinstance(raw_system, str):
            raise InvariantViolation("provider request hook returned invalid system prompt")
        if not isinstance(raw_tools, list) or any(
            not isinstance(schema, dict) for schema in raw_tools
        ):
            raise InvariantViolation("provider request hook returned invalid tool schemas")
        prepared_messages = cast(list[dict[str, Any]], raw_messages)
        prepared_system = raw_system or ""
        prepared_tools = cast(list[dict[str, Any]], raw_tools)
        request_snapshot = RequestSnapshot.create(
            messages=prepared_messages,
            system=prepared_system,
            tool_schemas=prepared_tools,
            metadata={
                **self._request_metadata,
                "thinking": thinking_override or "",
                "supports_parallel_tools": self._supports_parallel_tools,
                "supports_images": self._supports_images,
            },
        )
        if self._transcript is not None:
            ledger_seq = self._transcript.append_request_snapshot(
                context.step,
                request_snapshot,
            )
            recorded_snapshot = self._transcript.latest_request_snapshot()
            if recorded_snapshot is None:
                raise InvariantViolation("request snapshot was not persisted")
            validate_request_snapshot(recorded_snapshot, request_snapshot)
            await self._bus.publish(
                LlmRequestPreparedEvent(
                    run_id=context.run_id,
                    step=context.step,
                    ledger_seq=ledger_seq,
                    request_snapshot_digest=request_snapshot.digest,
                    preset_id=str(self._request_metadata.get("preset_id", "standard")),
                    preset_digest=str(self._request_metadata.get("preset_digest", "")),
                    route_id=request_snapshot.route_id,
                    model=request_snapshot.model,
                    wire_format=request_snapshot.wire_format,
                    execution_contract_digest=request_snapshot.execution_contract_digest,
                    ts=_now(),
                )
            )
        try:
            while True:
                attempt_bus = AttemptBus(self._bus)
                attempt_number = retries + 1
                self._append_audit(context, "llm.attempt_started", {
                    "attempt": attempt_number, "request_snapshot_digest": request_snapshot.digest,
                    "policy": asdict(self._retry_policy),
                })

                # 每次从冻结快照重建，适配器无法改变下一次重试的入参
                async def _attempt(monitored_bus: EventBus) -> LlmResponse:
                    return await self._provider.chat(
                        messages=deepcopy([
                            dict[str, object](m) for m in request_snapshot.messages
                        ]),
                        tool_schemas=deepcopy([
                            dict[str, object](s) for s in request_snapshot.tool_schemas
                        ]),
                        bus=monitored_bus, run_id=context.run_id, step=context.step,
                        system=request_snapshot.system, thinking=thinking_override,
                    )

                try:
                    response = await self._watchdog.run(_attempt, attempt_bus)
                    if self._provider_response_sink is not None:
                        await self._provider_response_sink(response)
                    if self._is_no_content(response):
                        raise NoContentResponseError(
                            "provider returned no text, reasoning, or tool calls"
                        )
                except asyncio.CancelledError:
                    await self._finish_attempt(
                        context, attempt_number, request_snapshot.digest,
                        attempt_bus, "cancelled", "cancelled",
                    )
                    raise
                except Exception as exc:
                    code = (
                        exc.failure_code if isinstance(exc, ProviderRequestError) else
                        "empty_response" if isinstance(exc, NoContentResponseError) else
                        "timeout" if isinstance(
                            exc, (StreamIdleTimeoutError, StreamWallTimeoutError),
                        )
                        else "transient_error" if self._is_transient_error(exc) else "request_error"
                    )
                    await self._finish_attempt(
                        context, attempt_number, request_snapshot.digest,
                        attempt_bus, "failed", code,
                    )
                    delay = self._retry_policy.delay(
                        retries + 1,
                        exc.retry_after_s if isinstance(exc, ProviderRequestError) else None,
                    )
                    if retries >= self._retry_policy.max_retries or delay is None or not (
                        self._is_transient_error(exc) or isinstance(exc, NoContentResponseError)
                    ):
                        raise
                    retries += 1
                    event = LlmRetryEvent(
                        run_id=context.run_id, step=context.step,
                        kind=(
                            "no_content" if isinstance(exc, NoContentResponseError) else "transient"
                        ),
                        attempt=retries, reason=code, failure_code=code,
                        delay_ms=round(delay * 1000), max_retries=self._retry_policy.max_retries,
                        request_snapshot_digest=request_snapshot.digest, ts=_now(),
                    )
                    event.ledger_seq = self._append_audit(
                        context, "llm.retry", event.model_dump(mode="json"),
                    )
                    await self._bus.publish(event)
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        self._append_audit(context, "llm.retry_cancelled", {
                            "attempt": retries, "request_snapshot_digest": request_snapshot.digest,
                        })
                        raise
                    self._append_audit(context, "llm.retry_started", {
                        "attempt": retries, "request_snapshot_digest": request_snapshot.digest,
                    })
                    continue

                failed = response.completion_status in {
                    "transport_error", "failed", "cancelled", "incomplete", "content_filtered",
                }
                if failed:
                    attempt_bus.record_stream_fragment({"response": asdict(response)})
                await self._finish_attempt(
                    context, attempt_number, request_snapshot.digest, attempt_bus,
                    "failed" if failed else "succeeded",
                    str(response.completion_status) if failed else "",
                )
                if failed:
                    # 不把断流的半条消息带入下一轮正式历史，也不执行部分工具调用
                    response.tool_calls = []
                    response.thinking_blocks = []
                context.clear_transient_context()
                return self._normalize_stop_reason(response)
        finally:
            if flushed_images:
                self._placeholder_flushed_images(context)

    # 审计提交失败必须显式终止请求，不能继续广播正常运行
    def _append_audit(
        self, context: ExecutionContext, event_type: str, payload: dict[str, object],
    ) -> int | None:
        if self._transcript is None:
            return None
        try:
            return self._transcript.append_audit(context.step, event_type, payload)
        except OSError as exc:
            raise InvariantViolation("attempt audit could not be persisted") from exc

    # 完成尝试记录先落账再广播，失败片段只放审计或 Artifact，不参与消息投影
    async def _finish_attempt(
        self, context: ExecutionContext, attempt: int, digest: str, bus: AttemptBus,
        status: Literal["succeeded", "failed", "cancelled"], failure_code: str,
    ) -> None:
        event = LlmAttemptFinishedEvent(
            run_id=context.run_id, step=context.step, attempt=attempt, status=status,
            failure_code=failure_code, request_snapshot_digest=digest, ts=_now(),
        )
        payload: dict[str, object] = event.model_dump(mode="json")
        if status != "succeeded":
            payload["partial_output_truncated"] = bus.truncated
            if self._artifact_store is not None and bus.bytes > 64 * 1024:
                artifact = await self._artifact_store.put(
                    json.dumps(bus.fragments, ensure_ascii=False), media_type="application/json",
                )
                payload["partial_output_artifact"] = artifact.model_dump(mode="json")
            else:
                payload["partial_output"] = bus.fragments
        event.ledger_seq = self._append_audit(context, "llm.attempt_finished", payload)
        await self._bus.publish(event)

    # 把工具登记的图片块注入下一条 user 消息，仅随下一次模型请求发送
    def _flush_pending_images(self, context: ExecutionContext) -> int:
        if not context.pending_images:
            return 0
        if not getattr(self, "_supports_images", True):
            raise RouteCapabilityError("images")
        blocks = list(context.pending_images)
        context.pending_images = []
        context.messages.append({"role": "user", "content": blocks})
        return len(blocks)

    # PLAN 模式且 route 已启用 thinking 时返回 high 档覆盖，Act 保持 route 配置
    def _thinking_override_for(self, context: ExecutionContext) -> str | None:
        if self._escalate_plan_thinking and context.runtime_mode == RuntimeMode.PLAN:
            return "high"
        return None

    # 步数耗尽时尝试续跑：先消耗自动续段配额，再经结构化提问征求用户；失败返回 False
    async def _try_continue_past_max_steps(self, context: ExecutionContext) -> bool:
        # 首次触达时固定初始配额，续段按固定段追加而不是随上限翻倍
        if self._initial_max_steps is None:
            self._initial_max_steps = max(1, context.max_steps)
        budget = self._initial_max_steps
        if self._step_continues_used < self._auto_step_continues:
            self._step_continues_used += 1
            context.max_steps += budget
            return True
        if (
            self._interaction_manager is not None
            and self._session_id
            and self._step_continues_used
            < self._auto_step_continues + _MAX_ASK_STEP_CONTINUES
        ):
            answer = await self._interaction_manager.ask(
                run_id=context.run_id,
                session_id=self._session_id,
                question=(
                    f"已达到本 run 的步数上限（{context.step} 步），任务尚未完成。"
                    f"是否继续执行最多 {budget} 步？"
                ),
                header="步数上限",
                options=["继续执行", "就此停止"],
                multi_select=False,
            )
            if answer.strip() == "继续执行":
                self._step_continues_used += 1
                context.max_steps += budget
                return True
        return False

    # 请求结束后用文本占位符替换已发送的图片消息，避免 base64 永久占据历史
    def _placeholder_flushed_images(self, context: ExecutionContext) -> None:
        if not context.messages:
            return
        last = context.messages[-1]
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            last["content"] = (
                "[image(s) from read_image were delivered to the model with the "
                "previous request; pixels omitted from history to save context]"
            )

    # 按统一完成状态归一化兼容 stop_reason，旧 provider 继续使用原有容错规则
    def _normalize_stop_reason(self, response: LlmResponse) -> LlmResponse:
        status = response.completion_status
        if status is not None:
            if status == "tool_use":
                response.stop_reason = "tool_use"
            elif status == "completed":
                response.stop_reason = "end_turn"
            elif status == "length":
                response.stop_reason = "max_tokens"
            else:
                response.stop_reason = status
            return response
        if response.tool_calls and response.stop_reason == "max_tokens":
            return response
        if response.tool_calls:
            response.stop_reason = "tool_use"
        elif response.stop_reason != "end_turn":
            response.stop_reason = "end_turn"
        return response

    # 计算 system prompt：context 已加载 base 后追加 todos 软状态摘要（若有）
    def _render_system(self, context: ExecutionContext) -> str:
        tools = self._registry.tool_schemas() if self._supports_tools else []
        base = context.system_prompt(build_system_prompt(self._registry.prompt_tools(tools)))
        route_id = str(self._request_metadata.get("route_id", "")).strip()
        model = str(self._request_metadata.get("model", "")).strip()
        thinking = str(self._request_metadata.get("thinking", "off")).strip() or "off"
        if route_id or model:
            base += (
                "\n\nRuntime identity (authoritative for this turn):\n"
                f"- Provider route: {route_id or '(not configured)'}\n"
                f"- Model: {model or '(not configured)'}\n"
                f"- Thinking level: {thinking}\n"
                "Answer questions about your current provider or model directly from these "
                "facts; do not inspect files or run a command to rediscover them."
            )
        if self._todo_state is None or not any(tool.get("name") == "tasks" for tool in tools):
            return base
        summary = self._todo_state.active_summary()
        if not summary:
            return base
        return base + "\n\n" + summary

    # 对上下文内工具结果应用确定性头尾裁剪并保留 Artifact 回查入口
    async def _apply_tool_result_budget(self, context: ExecutionContext) -> None:
        context.messages = truncate_tool_results(
            context.messages,
            limit=self._tool_result_limit,
            keep=self._tool_result_keep,
        )

    # 返回可参与并行批次的资源 claim；未知或不完整声明保守返回 None
    def _parallel_claims(self, tc: ToolCallBlock) -> tuple[ResourceClaim, ...] | None:
        try:
            resolved = self._registry.resolve_call(tc.name, dict(tc.input))
            if resolved.effective_parallel_policy == ParallelPolicy.SERIAL:
                return None
            if resolved.effective_parallel_policy == ParallelPolicy.SAFE:
                return () if not resolved.action.is_mutating else None
            claims = self._registry.resource_claims(tc.name, dict(tc.input))
        except (ToolCatalogError, PermissionError, ValueError, OSError):
            return None
        return claims or None

    # 判断两个资源 claim 是否因路径相交且至少一方独占而冲突
    @staticmethod
    def _claims_conflict(left: ResourceClaim, right: ResourceClaim) -> bool:
        if not left.exclusive and not right.exclusive:
            return False
        left_path = left.resource.removesuffix("/**")
        right_path = right.resource.removesuffix("/**")
        return (
            left_path == right_path
            or left.resource.endswith("/**")
            and right_path.startswith(left_path)
            or right.resource.endswith("/**")
            and left_path.startswith(right_path)
        )

    # 判断候选调用的 claims 是否可加入当前并行批次
    def _can_join_parallel_batch(
        self,
        claims: tuple[ResourceClaim, ...],
        batch_claims: list[ResourceClaim],
    ) -> bool:
        return not any(
            self._claims_conflict(candidate, existing)
            for candidate in claims
            for existing in batch_claims
        )

    # 判断一次调用是否可能改变可读状态，mutation 前清空读取缓存
    def _is_mutating_call(self, tc: ToolCallBlock) -> bool:
        try:
            return self._registry.resolve_call(
                tc.name,
                dict(tc.input),
            ).action.is_mutating
        except (ToolCatalogError, PermissionError, ValueError, OSError):
            return False

    # 为复用的只读结果补齐 started/finished 事件，保持工具事件配对
    async def _publish_cached_call(
        self,
        tc: ToolCallBlock,
        context: ExecutionContext,
        result: ToolResult,
    ) -> None:
        await self._bus.publish(
            ToolCallStartedEvent(
                run_id=context.run_id,
                tool_use_id=tc.id,
                tool_name=tc.name,
                operation_id=tc.id,
                params=dict(tc.input),
                ts=_now(),
            )
        )
        await self._bus.publish(
            ToolCallFinishedEvent(
                run_id=context.run_id,
                tool_use_id=tc.id,
                tool_name=tc.name,
                operation_id=tc.id,
                elapsed_ms=0,
                output=result.content,
                ts=_now(),
            )
        )

    # 从工具参数和结构化结果中提取工作区路径，patch 支持多文件结果
    @staticmethod
    def _tool_paths(tc: ToolCallBlock, result: ToolResult) -> tuple[str, ...]:
        paths: list[str] = []
        raw_path = tc.input.get("path")
        if isinstance(raw_path, str) and raw_path:
            paths.append(raw_path)
        try:
            payload = json.loads(result.content)
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            result_path = payload.get("path")
            if isinstance(result_path, str) and result_path:
                paths.append(result_path)
            files = payload.get("files")
            if isinstance(files, list):
                for item in files:
                    if isinstance(item, dict) and isinstance(item.get("path"), str):
                        paths.append(str(item["path"]))
        return tuple(dict.fromkeys(path.replace("\\", "/") for path in paths))

    # 从 read/edit/write 结果中提取可选内容 hash，不保留正文
    @staticmethod
    def _tool_content_hash(result: ToolResult) -> str:
        match = _CONTENT_HASH_RE.search(result.content[:4_000])
        return match.group(1) if match is not None else ""

    # 按 ToolSpec capability 判断工作集来源，非文件读写工具不纳入
    def _working_set_source(self, tc: ToolCallBlock) -> WorkingSetSource | None:
        if "path" not in tc.input and tc.name not in {"File", "apply_patch"}:
            return None
        try:
            resolved = self._registry.resolve_call(tc.name, dict(tc.input))
        except (ToolCatalogError, PermissionError, ValueError, OSError):
            return None
        return "edit" if resolved.action.is_mutating else "read"

    # 更新 working set，并在成功修改 Python 文件后注入一次性有界诊断
    async def _update_context_after_tool(
        self,
        tc: ToolCallBlock,
        result: ToolResult,
        context: ExecutionContext,
    ) -> None:
        if result.is_error:
            return
        source = self._working_set_source(tc)
        if source is None:
            return
        paths = self._tool_paths(tc, result)
        if not paths:
            return
        content_hash = self._tool_content_hash(result)
        for path in paths:
            context.working_set.touch(
                path,
                source,
                step=context.step,
                content_hash=content_hash,
            )

        if source == "edit" and self._diagnostics_client is not None:
            diagnostics_started = time.monotonic()
            report = await self._diagnostics_client.diagnose(list(paths))
            diagnostics_duration_ms = int(
                (time.monotonic() - diagnostics_started) * 1000
            )
            for diagnostic in report.diagnostics:
                context.working_set.touch(
                    diagnostic.path,
                    "diagnostic",
                    step=context.step,
                )
            rendered = report.render_context()
            if rendered:
                existing = context.transient_context.strip()
                context.set_transient_context(
                    f"{existing}\n\n{rendered}" if existing else rendered
                )
            await self._bus.publish(
                LspDiagnosticsEvent(
                    run_id=context.run_id,
                    step=context.step,
                    status=report.status,
                    tool=report.tool,
                    paths=list(paths),
                    diagnostic_count=len(report.diagnostics),
                    duration_ms=diagnostics_duration_ms,
                    truncated=report.truncated,
                    error=report.error,
                    ts=_now(),
                )
            )

        await self._bus.publish(
            ContextWorkingSetEvent(
                run_id=context.run_id,
                step=context.step,
                paths=[entry.path for entry in context.working_set.snapshot()],
                ts=_now(),
            )
        )

    # 将 Run 工具的真实结果转换为可持久化的结构化验证事件
    async def _publish_verification(
        self,
        tc: ToolCallBlock,
        result: ToolResult,
        context: ExecutionContext,
    ) -> None:
        if tc.name not in {"Run", "run_tests", "run_verifiers"}:
            return
        action = str(tc.input.get("action", ""))
        if not action:
            action = "tests" if tc.name == "run_tests" else "verifiers"
        try:
            raw_payload = json.loads(result.content)
        except (json.JSONDecodeError, TypeError):
            raw_payload = {}
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        raw_gates = payload.get("gates")
        gates: list[dict[str, Any]] = []
        if isinstance(raw_gates, list):
            for raw_gate in raw_gates[:8]:
                if not isinstance(raw_gate, dict):
                    continue
                gates.append(
                    {
                        "name": str(raw_gate.get("name", ""))[:80],
                        "command": str(raw_gate.get("command", ""))[:1_000],
                        "status": str(raw_gate.get("status", "unknown"))[:20],
                        "duration_ms": _nonnegative_int(raw_gate.get("duration_ms")),
                        "output_truncated": bool(raw_gate.get("output_truncated", False)),
                        "candidate_id": str(raw_gate.get("candidate_id", ""))[:64],
                        "source": str(raw_gate.get("source", ""))[:500],
                        "verification_eligible": bool(
                            raw_gate.get("verification_eligible", False)
                        ),
                    }
                )
        gate_count = max(
            1,
            _nonnegative_int(
                payload.get("gate_count"),
                default=len(gates) or 1,
            ),
        )
        failed = _nonnegative_int(
            payload.get("failed"),
            default=int(result.is_error),
        )
        passed = _nonnegative_int(
            payload.get("passed"),
            default=gate_count - failed,
        )
        paths = [
            entry.path
            for entry in context.working_set.snapshot()
            if "edit" in entry.sources
        ]
        common: dict[str, Any] = {
            "run_id": context.run_id,
            "step": context.step,
            "tool": tc.name,
            "action": action,
            "gate_count": gate_count,
            "passed": passed,
            "failed": failed,
            "paths": paths,
            "gates": gates,
            "ts": _now(),
        }
        verification_eligible = (
            payload.get("verification_eligible") is True
            and payload.get("verification_source") == "manifest_declared"
        )
        if not verification_eligible:
            await self._bus.publish(
                VerificationFailedEvent(
                    **{**common, "failed": max(1, failed)},
                    failure_class="untrusted_verification_command",
                )
            )
            return
        if result.is_error or failed > 0:
            await self._bus.publish(
                VerificationFailedEvent(
                    **{**common, "failed": max(1, failed)},
                    failure_class=result.error_type or "verification_failed",
                )
            )
            return
        await self._bus.publish(VerificationCompletedEvent(**common))

    # 将原生循环的工具调用送入统一权限与执行管线。
    async def _invoke_one(
        self, tc: ToolCallBlock, context: ExecutionContext,
        *, prepared_arguments: PreparedToolArguments | None = None,
    ) -> ToolResult:
        await self._cancellation_checkpoint()
        result = await invoke_tool(
            self._registry, tc, self._bus, context.run_id,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            hooks=self._hooks,
            artifact_store=self._artifact_store,
            authority_snapshot=self._authority_snapshot,
            step=context.step,
            prepared_arguments=prepared_arguments,
        )
        await self._cancellation_checkpoint()
        return result

    # 把单个 ToolResult 按原顺序写回 context/transcript；返回是否命中 permission_required
    async def _record_result(
        self,
        idx: int,
        block_count: int,
        tc: ToolCallBlock,
        result: ToolResult,
        context: ExecutionContext,
    ) -> bool:
        model_content = result.model_content()
        context.add_tool_result(tc.id, model_content, is_error=result.is_error)
        if self._transcript is not None:
            self._transcript.append_tool_result(
                context.step,
                tc.id,
                model_content,
                is_error=result.is_error,
                block_index=idx,
                block_count=block_count,
            )
        await self._update_context_after_tool(tc, result, context)
        await self._publish_verification(tc, result, context)
        if result.error_type == "permission_required":
            context.mark_failed("permission_required")
            return True
        if not context.is_done():
            stuck = self._stuck_guard.observe(tc, result)
            if stuck is not None:
                self._pending_repeat_notices.append((
                    AgentRepeatNoticeEvent(
                        run_id=context.run_id, step=context.step,
                        tool_name=stuck.tool_name, signature=stuck.signature,
                        repeat_count=stuck.repeat_count, ts=_now(),
                    ),
                    stuck.notice,
                ))
        return False

    # 在整批工具结果配对后追加提醒，禁止提醒把一次工具响应拆成两段
    async def _flush_repeat_notices(self, context: ExecutionContext) -> None:
        notices, self._pending_repeat_notices = self._pending_repeat_notices, []
        for event, content in notices:
            if self._transcript is not None:
                self._transcript.append_notice(context.step, content)
            context.messages.append({"role": "user", "content": content})
            await self._bus.publish(event)

    # 驱动循环并保证任何退出路径都为已开始的步骤补齐终止事件
    async def run(self, context: ExecutionContext) -> None:
        self._active_step = 0
        try:
            from code_rook.core.agent_runtime.driver import execute_context

            await execute_context(self, context)
        finally:
            if self._active_step:
                finish_task = asyncio.create_task(
                    self._finish_active_step(context),
                    name=f"finish-step:{context.run_id}:{self._active_step}",
                )
                try:
                    await asyncio.shield(finish_task)
                except asyncio.CancelledError:
                    await finish_task
                    raise

    # 原子取得并清除活动步骤后发布结束事件，避免发布失败时重复补发
    async def _finish_active_step(self, context: ExecutionContext) -> None:
        step = self._active_step
        if not step:
            return
        self._active_step = 0
        await self._bus.publish(
            StepFinishedEvent(run_id=context.run_id, step=step, ts=_now())
        )

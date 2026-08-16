from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.events import (
    AgentDecisionEvent,
    AgentStuckEvent,
    ContextBudgetEvent,
    ContextPrefixFingerprintEvent,
    ContextWorkingSetEvent,
    LlmRetryEvent,
    LspDiagnosticsEvent,
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from code_rook.core.compact.budget import distill_tool_results, truncate_tool_results
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.lsp import WorkspaceDiagnosticsClient
from code_rook.core.prefix_fingerprint import PrefixFingerprintTracker
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ParallelPolicy, ResourceClaim, ToolCatalogError
from code_rook.core.turn import (
    NoContentResponseError,
    ReadRepeatGuard,
    StreamWatchdog,
    StreamWatchdogError,
    StuckGuard,
)
from code_rook.core.working_set import WorkingSetSource

if TYPE_CHECKING:
    from code_rook.core.artifacts import ArtifactStore
    from code_rook.core.compact.compactor import Compactor
    from code_rook.core.hooks import HookManager
    from code_rook.core.interaction import InteractionManager
    from code_rook.core.permissions.manager import PermissionManager
    from code_rook.core.task.manager import TodoStateView


log = logging.getLogger(__name__)

_CONTEXT_ERROR_MARKERS = (
    "context_length_exceeded",
    "max_context_window",
    "prompt is too long",
    "prompt too long",
    "too many tokens",
)
_TRANSIENT_ERROR_MARKERS = ("429", "529", "rate limit", "overloaded", "temporarily unavailable")
_INSPECTION_TOOLS = {
    "agent_result",
    "background_list",
    "background_result",
    "checkpoint_list",
    "git_diff",
    "glob",
    "grep",
    "list_dir",
    "memory_search",
    "read_file",
    "task_get",
    "task_list",
    "worktree_list",
}
_PLANNING_TOOLS = {"task_claim", "task_create", "task_update", "update_plan"}
_CHANGE_TOOLS = {
    "apply_patch",
    "checkpoint_rewind",
    "edit_file",
    "memory_forget",
    "memory_save",
    "note_save",
    "write_file",
    "worktree_create",
    "worktree_remove",
}
_DELEGATION_TOOLS = {"spawn_agent"}

# 基础系统提示；提供对话纠错、能力校验和工具路由规则，todos 摘要会追加在其后
_BASE_SYSTEM_PROMPT = (
    "You are CodeRook, a local agentic coding assistant. "
    "Interpret each request using the full conversation and the runtime environment below. "
    "Before acting, infer the user's objective, target, scope, requested operation, and the "
    "evidence needed to verify the result. Do not expose this internal frame unless useful. "
    "Treat later clarifications and corrections as higher-priority evidence. If they change the "
    "objective, target, or scope, discard incompatible assumptions and reselect tools. "
    "Choose tools from their documented capabilities and the runtime environment, not from "
    "surface word overlap between the request and a tool name. "
    "When multiple interpretations remain plausible, use conversation context and safe, "
    "low-cost read-only inspection to resolve them; otherwise ask one focused clarification. "
    "Use the available tools to complete the user's actual goal. "
    "Keep reasoning out of ordinary response text; reasoning-capable providers expose it "
    "through a separate reasoning channel. Do not emit progress narration before tool calls. "
    "Call the required tools directly. "
    "For complex work with at least three meaningful actions, call tasks with the create and "
    "update actions to maintain concise, verifiable work items. Skip task tracking for simple "
    "requests. "
    "Never use emoji, decorative symbols, filler greetings, or redundant recaps in "
    "user-visible text. Prefer short factual prose. "
    "Before claiming that you cannot inspect or perform something, check the available tool "
    "schemas and runtime environment. If a capable tool requires approval, request or explain "
    "that approval instead of claiming the capability does not exist. Never invent tool results. "
    "For factual or current-state questions, base conclusions only on successful tool "
    "output. A failed, denied, or unavailable check is unknown, not evidence that an item is "
    "absent. Report material evidence gaps and do not infer relationships that observations did "
    "not establish. Start with broad, low-cost checks, then narrow only to resolve remaining "
    "gaps; avoid redundant probes. "
    "Use bash when the task requires local command-line or operating-system capabilities. "
    "Prefer glob and grep over shell commands for code discovery. "
    "Prefer edit_file over write_file when changing an existing file. "
    "Use apply_patch for related changes across multiple files. "
    "Call memory with the save action for durable project facts, user preferences, and reusable "
    "debugging discoveries; do not store secrets. "
    "Use background_start for slow tests or builds, then poll with "
    "background_result while continuing independent work. "
    "File changes are checkpointed automatically; use "
    "checkpoint_rewind "
    "when the user asks to undo the latest agent change. "
    "When the goal is fully achieved, respond with a final answer "
    "and do not call any more tools."
)

# 当 todos 未完成却 end_turn 时注入给模型的提醒，强制其继续推进或显式更新 todos
_TODO_END_TURN_REMINDER = (
    "You ended the turn, but the Todo State above still has incomplete items. "
    "Either continue working on the next pending/in_progress todo, or call tasks with "
    "action='update' and status='completed' for any items that are truly done, then end."
)
# 连续最多推迟次数；超过即视为模型不再推进 todos，放弃阻拦让其结束
_MAX_TODO_DEFERS = 3
_MAX_TRANSIENT_RETRIES = 2
_MAX_NO_CONTENT_RETRIES = 2
_CONTENT_HASH_RE = re.compile(
    r"(?:content_hash[=:]\s*|\"(?:new_hash|content_hash)\"\s*:\s*\")([^\s,\")]+)"
)


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 根据模型实际选择的工具归纳当前动作意图，不另发起分类模型请求
def _decision_intent(tool_calls: list[ToolCallBlock]) -> str:
    names = {call.name for call in tool_calls}
    if not names:
        return "respond"
    agent_actions = {
        str(call.input.get("action", ""))
        for call in tool_calls
        if call.name == "agent"
    }
    memory_actions = {
        str(call.input.get("action", ""))
        for call in tool_calls
        if call.name == "memory"
    }
    task_actions = {
        str(call.input.get("action", ""))
        for call in tool_calls
        if call.name == "tasks"
    }
    if agent_actions & {"start", "cancel", "followup"}:
        return "delegate"
    if names & _DELEGATION_TOOLS:
        return "delegate"
    if names & _CHANGE_TOOLS or memory_actions & {"save", "forget"}:
        return "change"
    if names & _PLANNING_TOOLS or task_actions & {"create", "claim", "update"}:
        return "plan"
    if (
        names <= (_INSPECTION_TOOLS | {"agent", "memory", "tasks"})
        and agent_actions <= {"status", "peek", "wait"}
        and memory_actions <= {"search"}
        and task_actions <= {"list", "get"}
    ):
        return "inspect"
    return "execute"


# 判断最新用户消息是否以中文为主，供无模型文本时选择本地化回退
def _uses_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


# 从用户可见文本生成单行进度摘要；无文本时按实际工具和用户语言给出回退
def _decision_summary(
    text: str,
    tool_calls: list[ToolCallBlock],
    user_text: str,
) -> str:
    visible = " ".join(text.split())
    if visible:
        return visible if len(visible) <= 240 else visible[:237] + "..."
    names = list(dict.fromkeys(call.name for call in tool_calls))
    if names:
        prefix = "调用工具：" if _uses_chinese(user_text) else "Using "
        return prefix + ", ".join(names)
    return "准备回答" if _uses_chinese(user_text) else "Preparing the response"


class TranscriptSink(Protocol):
    def append_assistant(self, step: int, blocks: list[dict[str, object]]) -> None: ...

    def append_user(self, step: int, content: str) -> None: ...

    def append_tool_result(
        self,
        step: int,
        tool_use_id: str,
        content: str,
        *,
        is_error: bool,
        block_index: int,
        block_count: int,
    ) -> None: ...


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
        diagnostics_client: WorkspaceDiagnosticsClient | None = None,
        prefix_tracker: PrefixFingerprintTracker | None = None,
        escalate_plan_thinking: bool = False,
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
        self._retry_backoff_s = retry_backoff_s
        self._diagnostics_client = diagnostics_client
        self._prefix_tracker = prefix_tracker or PrefixFingerprintTracker()
        self._escalate_plan_thinking = escalate_plan_thinking
        self._reactive_compaction_attempted = False
        # 防 end_turn 早退 reminder 防抖：跟踪 todos 摘要快照与已提醒次数
        self._last_todo_snapshot: str = ""
        self._end_turn_defer_count: int = 0

    # 判断异常是否表示上下文窗口已超限
    @staticmethod
    def _is_context_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in _CONTEXT_ERROR_MARKERS)

    # 判断异常是否适合短暂退避后重试
    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        message = f"{type(exc).__name__} {exc}".lower()
        return any(marker in message for marker in _TRANSIENT_ERROR_MARKERS)

    # 判断 provider 是否返回了既无正文、推理也无工具调用的空响应
    @staticmethod
    def _is_no_content(response: LlmResponse) -> bool:
        return not (
            response.text.strip()
            or response.tool_calls
            or response.thinking_blocks
        )

    # 显式让出一次事件循环，使已请求的任务取消在启动下一操作前生效
    @staticmethod
    async def _cancellation_checkpoint() -> None:
        await asyncio.sleep(0)

    # 在 watchdog 下调用 Provider，并分别统计 transient 与 no-content 重试
    async def _call_provider(self, context: ExecutionContext) -> LlmResponse:
        transient_retries = 0
        no_content_retries = 0
        system_prompt = self._render_system(context)
        tool_schemas = self._registry.tool_schemas()
        await self._bus.publish(
            ContextBudgetEvent(
                run_id=context.run_id,
                step=context.step,
                message_tokens=max(
                    1,
                    sum(len(str(message.get("content", ""))) for message in context.messages)
                    // 4,
                ),
                system_tokens=max(1, len(system_prompt) // 4),
                tool_schema_tokens=max(1, len(json.dumps(tool_schemas, sort_keys=True)) // 4),
                tool_count=len(tool_schemas),
                ts=_now(),
            )
        )
        prefix = self._prefix_tracker.observe(
            system_prompt=context.stable_system_prompt(_BASE_SYSTEM_PROMPT),
            tool_catalog=self._registry.canonical_catalog_json(),
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
        try:
            while True:
                # 使用 watchdog 提供的监控 bus 调用同一个 Provider 请求
                async def _attempt(monitored_bus: EventBus) -> LlmResponse:
                    return await self._provider.chat(
                        messages=context.messages,
                        tool_schemas=tool_schemas,
                        bus=monitored_bus,
                        run_id=context.run_id,
                        step=context.step,
                        system=system_prompt,
                        thinking=self._thinking_override_for(context),
                    )

                try:
                    response = await self._watchdog.run(_attempt, self._bus)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if (
                        transient_retries >= _MAX_TRANSIENT_RETRIES
                        or not self._is_transient_error(exc)
                    ):
                        raise
                    transient_retries += 1
                    await self._bus.publish(
                        LlmRetryEvent(
                            run_id=context.run_id,
                            step=context.step,
                            kind="transient",
                            attempt=transient_retries,
                            reason=str(exc),
                            ts=_now(),
                        )
                    )
                    if self._retry_backoff_s > 0:
                        await asyncio.sleep(
                            self._retry_backoff_s * (2 ** (transient_retries - 1))
                        )
                    else:
                        await self._cancellation_checkpoint()
                    continue

                if not self._is_no_content(response):
                    context.clear_transient_context()
                    return self._normalize_stop_reason(response)
                if no_content_retries >= _MAX_NO_CONTENT_RETRIES:
                    raise NoContentResponseError(
                        "provider returned no text, reasoning, or tool calls"
                    )
                no_content_retries += 1
                await self._bus.publish(
                    LlmRetryEvent(
                        run_id=context.run_id,
                        step=context.step,
                        kind="no_content",
                        attempt=no_content_retries,
                        reason="provider returned no text, reasoning, or tool calls",
                        ts=_now(),
                    )
                )
        finally:
            if flushed_images:
                self._placeholder_flushed_images(context)

    # 把工具登记的图片块注入下一条 user 消息，仅随下一次模型请求发送
    def _flush_pending_images(self, context: ExecutionContext) -> int:
        if not context.pending_images:
            return 0
        blocks = list(context.pending_images)
        context.pending_images = []
        context.messages.append({"role": "user", "content": blocks})
        return len(blocks)

    # PLAN 模式且 route 已启用 thinking 时返回 high 档覆盖，Act 保持 route 配置
    def _thinking_override_for(self, context: ExecutionContext) -> str | None:
        if self._escalate_plan_thinking and context.runtime_mode == RuntimeMode.PLAN:
            return "high"
        return None

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

    # 归一化非标准 stop_reason：带工具调用统一为 tool_use（max_tokens 除外），
    # 无工具调用的未知值（如兼容后端的 "stop"）统一为 end_turn，防止循环空转到 max_steps
    def _normalize_stop_reason(self, response: LlmResponse) -> LlmResponse:
        if response.tool_calls and response.stop_reason == "max_tokens":
            return response
        if response.tool_calls:
            response.stop_reason = "tool_use"
        elif response.stop_reason != "end_turn":
            response.stop_reason = "end_turn"
        return response

    # 计算 system prompt：context 已加载 base 后追加 todos 软状态摘要（若有）
    def _render_system(self, context: ExecutionContext) -> str:
        base = context.system_prompt(_BASE_SYSTEM_PROMPT)
        if self._todo_state is None:
            return base
        summary = self._todo_state.active_summary()
        if not summary:
            return base
        return base + "\n\n" + summary

    # 取当前 todos 摘要作为快照，用于判断"模型是否在两次 end_turn 间更新了 todos"
    def _todo_snapshot(self) -> str:
        return self._todo_state.active_summary() if self._todo_state else ""

    # 将运行中到达的用户纠偏按顺序注入下一次模型决策上下文
    def _inject_steering(self, context: ExecutionContext) -> bool:
        if self._interaction_manager is None:
            return False
        messages = self._interaction_manager.drain_steering(context.run_id)
        if not messages:
            return False
        content = (
            "User steering update received while this run was active. "
            "Treat the updates below as the newest instructions and revise the remaining work "
            "accordingly:\n\n"
            + "\n\n".join(messages)
        )
        context.messages.append({"role": "user", "content": content})
        if self._transcript is not None:
            self._transcript.append_user(context.step, content)
        return bool(messages)

    # 判断是否应推迟 end_turn：todo_state 存在、有未完成 todos、且快照自上次提醒已有
    # 变化或尚未提醒过；超过 _MAX_TODO_DEFERS 次仍无变化则放弃阻拦
    def _should_defer_end_turn(self) -> bool:
        if self._todo_state is None or not self._todo_state.has_incomplete():
            return False
        if self._end_turn_defer_count >= _MAX_TODO_DEFERS:
            return False
        # 上一轮注入 reminder 后，若 todos 摘要仍未变化，则视为模型未推进，不再阻拦
        snapshot = self._todo_snapshot()
        return snapshot != self._last_todo_snapshot

    # 对上下文内工具结果应用蒸馏与头尾截断的分级预算
    async def _apply_tool_result_budget(self, context: ExecutionContext) -> None:
        context.messages, _ = await distill_tool_results(
            context.messages,
            self._provider,
            threshold=self._tool_result_summarize_threshold,
            fallback_keep=self._tool_result_keep,
        )
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

    # 判断某个 tool_call 是否具备加入空并行批次的完整声明
    def _is_parallelable(self, tc: ToolCallBlock) -> bool:
        return self._parallel_claims(tc) is not None

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
                params=dict(tc.input),
                ts=_now(),
            )
        )
        await self._bus.publish(
            ToolCallFinishedEvent(
                run_id=context.run_id,
                tool_use_id=tc.id,
                tool_name=tc.name,
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
            report = await self._diagnostics_client.diagnose(list(paths))
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

    # 单一 tool_call 调用通道：屏蔽 _run_act_phase 与上层 run 对 invocation 签名的重复
    async def _invoke_one(self, tc: ToolCallBlock, context: ExecutionContext) -> ToolResult:
        await self._cancellation_checkpoint()
        read_key = self._read_guard.call_key(self._registry, tc)
        if read_key is not None:
            cached = self._read_guard.get(read_key)
            if cached is not None:
                await self._publish_cached_call(tc, context, cached)
                await self._cancellation_checkpoint()
                return cached
        elif self._is_mutating_call(tc):
            self._read_guard.clear()

        result = await invoke_tool(
            self._registry, tc, self._bus, context.run_id,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            hooks=self._hooks,
            artifact_store=self._artifact_store,
        )
        await self._cancellation_checkpoint()
        if read_key is not None:
            self._read_guard.put(read_key, result)
        return result

    # 同一并行批中只执行一次完全相同的只读调用，并为重复项回放配对事件
    async def _invoke_parallel_batch(
        self,
        batch: list[ToolCallBlock],
        context: ExecutionContext,
    ) -> list[ToolResult]:
        representatives: list[ToolCallBlock] = []
        representative_indexes: list[int] = []
        duplicate_of: dict[int, int] = {}
        seen: dict[str, int] = {}
        for index, tool_call in enumerate(batch):
            key = self._read_guard.call_key(self._registry, tool_call)
            if key is not None and key in seen:
                duplicate_of[index] = seen[key]
                continue
            if key is not None:
                seen[key] = index
            representatives.append(tool_call)
            representative_indexes.append(index)

        await self._cancellation_checkpoint()
        gathered = await asyncio.gather(
            *(self._invoke_one(tool_call, context) for tool_call in representatives)
        )
        results_by_index = {
            index: result
            for index, result in zip(representative_indexes, gathered, strict=True)
        }
        for index, source_index in duplicate_of.items():
            await self._cancellation_checkpoint()
            result = results_by_index[source_index]
            results_by_index[index] = result
            await self._publish_cached_call(batch[index], context, result)
        return [results_by_index[index] for index in range(len(batch))]

    # 把单个 ToolResult 按原顺序写回 context/transcript；返回是否命中 permission_required
    async def _record_result(
        self,
        idx: int,
        block_count: int,
        tc: ToolCallBlock,
        result: ToolResult,
        context: ExecutionContext,
    ) -> bool:
        context.add_tool_result(tc.id, result.content, is_error=result.is_error)
        if result.images:
            for image_block in result.images:
                context.add_pending_image(dict(image_block))
        if self._transcript is not None:
            self._transcript.append_tool_result(
                context.step,
                tc.id,
                result.content,
                is_error=result.is_error,
                block_index=idx,
                block_count=block_count,
            )
        await self._update_context_after_tool(tc, result, context)
        if result.error_type == "permission_required":
            context.mark_failed("permission_required")
            return True
        if not context.is_done():
            stuck = self._stuck_guard.observe(tc, result)
            if stuck is not None:
                await self._bus.publish(
                    AgentStuckEvent(
                        run_id=context.run_id,
                        step=context.step,
                        tool_name=stuck.tool_name,
                        signature=stuck.signature,
                        repeat_count=stuck.repeat_count,
                        ts=_now(),
                    )
                )
                context.mark_failed("stuck_repetition")
                return True
        return False

    # 执行一轮 tool_use 序列：连续的 can_parallel 工具组成一批用 asyncio.gather 并发，
    # 副作用工具按模型给定顺序串行；任一批中若出现 permission_required 立即停并为后续补合成结果
    async def _run_act_phase(
        self,
        tool_calls: list[ToolCallBlock],
        context: ExecutionContext,
    ) -> None:
        block_count = len(tool_calls)
        i = 0
        n = len(tool_calls)
        try:
            while i < n:
                await self._cancellation_checkpoint()
                j = i
                batch_claims: list[ResourceClaim] = []
                # 收集从 i 开始连续的并行工具，构造一个批
                while j < n:
                    claims = self._parallel_claims(tool_calls[j])
                    if claims is None or not self._can_join_parallel_batch(
                        claims,
                        batch_claims,
                    ):
                        break
                    batch_claims.extend(claims)
                    j += 1
                batch = tool_calls[i:j]

                if not batch:
                    # 当前 tool_calls[i] 不可并行（副作用或未知工具），单独串行执行
                    tc = tool_calls[i]
                    result = await self._invoke_one(tc, context)
                    if await self._record_result(i, block_count, tc, result, context):
                        self._fill_skipped_tool_results(
                            tool_calls, i + 1, block_count, context
                        )
                        return
                    i += 1
                    continue

                if len(batch) == 1:
                    results: list[ToolResult] = [await self._invoke_one(batch[0], context)]
                else:
                    results = await self._invoke_parallel_batch(batch, context)

                should_stop = False
                for k, tc in enumerate(batch):
                    should_stop = (
                        await self._record_result(
                            i + k,
                            block_count,
                            tc,
                            results[k],
                            context,
                        )
                        or should_stop
                    )
                if should_stop:
                    self._fill_skipped_tool_results(tool_calls, j, block_count, context)
                    return
                i = j
        except asyncio.CancelledError:
            self._fill_skipped_tool_results(tool_calls, i, block_count, context)
            raise

    # 为提前终止而未执行的 tool_use 追加合成错误结果，保证 transcript 中 tool 协议闭环
    def _fill_skipped_tool_results(
        self,
        tool_calls: list[ToolCallBlock],
        start_index: int,
        block_count: int,
        context: ExecutionContext,
    ) -> None:
        for idx in range(start_index, len(tool_calls)):
            tc = tool_calls[idx]
            error = (
                "Skipped: this tool call was not executed because the run "
                "terminated early."
            )
            context.add_tool_result(tc.id, error, is_error=True)
            if self._transcript is not None:
                self._transcript.append_tool_result(
                    context.step,
                    tc.id,
                    error,
                    is_error=True,
                    block_index=idx,
                    block_count=block_count,
                )

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            self._inject_steering(context)
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            await self._apply_tool_result_budget(context)

            # [plan] watchdog 内调用 LLM；重试计数与 context overflow 恢复彼此独立
            try:
                response = await self._call_provider(context)
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
            except StreamWatchdogError as exc:
                log.warning(
                    "LLM watchdog failed run_id=%s step=%d reason=%s",
                    context.run_id,
                    context.step,
                    exc.reason,
                )
                context.mark_failed(exc.reason)
                break
            except NoContentResponseError as exc:
                log.warning(
                    "LLM returned no content run_id=%s step=%d: %s",
                    context.run_id,
                    context.step,
                    exc,
                )
                context.mark_failed(exc.reason)
                break
            except Exception as exc:
                if (
                    self._is_context_error(exc)
                    and self._compactor is not None
                    and not self._reactive_compaction_attempted
                ):
                    self._reactive_compaction_attempted = True
                    compacted = await self._compactor.compact(
                        context,
                        self._provider,
                        focus="Preserve the current goal and recent tool-use pairs after overflow.",
                        trigger="overflow",
                    )
                    if compacted is not None:
                        await self._bus.publish(
                            StepFinishedEvent(
                                run_id=context.run_id,
                                step=context.step,
                                ts=_now(),
                            )
                        )
                        continue
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            await self._bus.publish(
                AgentDecisionEvent(
                    run_id=context.run_id,
                    step=context.step,
                    intent=_decision_intent(response.tool_calls),  # type: ignore[arg-type]
                    summary=_decision_summary(response.text, response.tool_calls, context.goal),
                    tool_names=[call.name for call in response.tool_calls],
                    has_visible_text=bool(response.text.strip()),
                    ts=_now(),
                )
            )

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)
            if self._transcript is not None:
                self._transcript.append_assistant(context.step, blocks)

            # [act] 按工具能力分组执行；连续的 can_parallel 工具组成一批并发，副作用工具串行
            if response.stop_reason == "tool_use":
                try:
                    await self._run_act_phase(response.tool_calls, context)
                except asyncio.CancelledError:
                    context.mark_failed("cancelled")
                    raise
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for result_index, tc in enumerate(response.tool_calls):
                    error = (
                        "Error: output token limit reached before this tool call could be "
                        "completed. Please break the task into smaller steps and try again."
                    )
                    context.add_tool_result(tc.id, error, is_error=True)
                    if self._transcript is not None:
                        self._transcript.append_tool_result(
                            context.step,
                            tc.id,
                            error,
                            is_error=True,
                            block_index=result_index,
                            block_count=len(response.tool_calls),
                        )

            steering_received = self._inject_steering(context)

            # 软状态机：end_turn 时若有未完成 todos 且 todos 自上次提醒发生过变化，注入 reminder
            # 让模型继续；连续 _MAX_TODO_DEFERS 次提醒 todos 仍不变就放弃阻拦，避免死循环
            if response.stop_reason == "end_turn":
                if steering_received:
                    pass
                elif self._should_defer_end_turn():
                    snapshot = self._todo_snapshot() if self._todo_state else ""
                    self._end_turn_defer_count += 1
                    context.messages.append(
                        {"role": "user", "content": _TODO_END_TURN_REMINDER}
                    )
                    if self._transcript is not None:
                        self._transcript.append_user(
                            context.step,
                            _TODO_END_TURN_REMINDER,
                        )
                    self._last_todo_snapshot = snapshot
                else:
                    context.result = response.text or ""
                    context.mark_success()
            elif not context.is_done() and context.step >= context.max_steps:
                context.mark_failed("exceeded_max_steps")

            # 工具结果追加完毕（messages 末尾为 user）后检查压缩，仅在 run 继续时触发
            # 此时压缩结果 [user_summary, assistant_ack] 对下一次 LLM 调用是合法输入
            if (
                not context.is_done()
                and response.stop_reason == "tool_use"
                and self._compactor is not None
                and self._compact_threshold > 0
                and response.usage is not None
                and response.usage.context_pct >= self._compact_threshold
            ):
                await self._apply_tool_result_budget(context)
                compacted = await self._compactor.compact(
                    context,
                    self._provider,
                    trigger="auto_threshold",
                )
                if compacted is not None and self._hooks is not None:
                    await self._hooks.emit(
                        "compaction_completed",
                        {
                            "run_id": context.run_id,
                            "session_id": self._session_id,
                            "trigger": "auto_threshold",
                            "summary_path": compacted.summary_path,
                            "saved_tokens": max(
                                0,
                                compacted.original_token_estimate - compacted.compacted_tokens,
                            ),
                        },
                    )

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

        if self._hooks is not None:
            await self._hooks.emit(
                "turn_stop",
                {
                    "run_id": context.run_id,
                    "session_id": self._session_id,
                    "status": context.status,
                    "reason": context.reason,
                    "result": context.result,
                },
            )

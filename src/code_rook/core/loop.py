from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from code_rook.core.bus.events import StepFinishedEvent, StepStartedEvent
from code_rook.core.compact.budget import distill_tool_results, truncate_tool_results
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from code_rook.core.compact.compactor import Compactor
    from code_rook.core.hooks import HookManager
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
    "Use memory_save for durable project facts, user preferences, and "
    "reusable debugging discoveries; do not store secrets. "
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
    "Either continue working on the next pending/in_progress todo, or call "
    "task_update(status='completed') for any items that are truly done, then end."
)
# 连续最多推迟次数；超过即视为模型不再推进 todos，放弃阻拦让其结束
_MAX_TODO_DEFERS = 3


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
    ) -> None:
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

    # 判断某个 tool_call 是否允许并行：tool 存在且声明 can_parallel=True（多为只读工具）
    def _is_parallelable(self, tc: ToolCallBlock) -> bool:
        tool = self._registry.get(tc.name)
        return tool is not None and tool.can_parallel

    # 单一 tool_call 调用通道：屏蔽 _run_act_phase 与上层 run 对 invocation 签名的重复
    async def _invoke_one(self, tc: ToolCallBlock, context: ExecutionContext) -> ToolResult:
        return await invoke_tool(
            self._registry, tc, self._bus, context.run_id,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            hooks=self._hooks,
        )

    # 把单个 ToolResult 按原顺序写回 context/transcript；返回是否命中 permission_required
    def _record_result(
        self,
        idx: int,
        block_count: int,
        tc: ToolCallBlock,
        result: ToolResult,
        context: ExecutionContext,
    ) -> bool:
        context.add_tool_result(tc.id, result.content, is_error=result.is_error)
        if self._transcript is not None:
            self._transcript.append_tool_result(
                context.step,
                tc.id,
                result.content,
                is_error=result.is_error,
                block_index=idx,
                block_count=block_count,
            )
        if result.error_type == "permission_required":
            context.mark_failed("permission_required")
            return True
        return False

    # 执行一轮 tool_use 序列：连续的 can_parallel 工具组成一批用 asyncio.gather 并发，
    # 副作用工具按模型给定顺序串行；任一批中若出现 permission_required 立即停并跳过后续
    async def _run_act_phase(
        self,
        tool_calls: list[ToolCallBlock],
        context: ExecutionContext,
    ) -> None:
        block_count = len(tool_calls)
        i = 0
        n = len(tool_calls)
        while i < n:
            j = i
            # 收集从 i 开始连续的并行工具，构造一个批
            while j < n and self._is_parallelable(tool_calls[j]):
                j += 1
            batch = tool_calls[i:j]

            if not batch:
                # 当前 tool_calls[i] 不可并行（副作用或未知工具），单独串行执行
                tc = tool_calls[i]
                result = await self._invoke_one(tc, context)
                if self._record_result(i, block_count, tc, result, context):
                    return
                i += 1
                continue

            if len(batch) == 1:
                results: list[ToolResult] = [await self._invoke_one(batch[0], context)]
            else:
                gathered = await asyncio.gather(
                    *(self._invoke_one(tc, context) for tc in batch)
                )
                results = list(gathered)

            for k, tc in enumerate(batch):
                if self._record_result(i + k, block_count, tc, results[k], context):
                    return
            i = j

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            await self._apply_tool_result_budget(context)

            # [plan] call LLM — transient errors retry; context overflow triggers one compaction
            try:
                for attempt in range(3):
                    try:
                        response = await self._provider.chat(
                            messages=context.messages,
                            tool_schemas=self._registry.tool_schemas(),
                            bus=self._bus,
                            run_id=context.run_id,
                            step=context.step,
                            system=self._render_system(context),
                        )
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        if attempt >= 2 or not self._is_transient_error(exc):
                            raise
                        await asyncio.sleep(0.5 * (2**attempt))
            except asyncio.CancelledError:
                context.mark_failed("cancelled")
                raise
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
                await self._run_act_phase(response.tool_calls, context)
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

            # 软状态机：end_turn 时若有未完成 todos 且 todos 自上次提醒发生过变化，注入 reminder
            # 让模型继续；连续 _MAX_TODO_DEFERS 次提醒 todos 仍不变就放弃阻拦，避免死循环
            if response.stop_reason == "end_turn":
                if self._should_defer_end_turn():
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
                await self._compactor.compact(
                    context,
                    self._provider,
                    trigger="auto_threshold",
                )

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

        if self._hooks is not None:
            await self._hooks.emit(
                "Stop",
                {
                    "run_id": context.run_id,
                    "session_id": self._session_id,
                    "status": context.status,
                    "reason": context.reason,
                    "result": context.result,
                },
            )

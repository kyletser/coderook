from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from code_rook.core.bus.events import (
    ContextCompactedEvent,
    ContextCompactionCommittedEvent,
    ContextCompactionStartedEvent,
    ContextCompactionSummaryEvent,
)
from code_rook.core.compact.models import (
    CompactionQuality,
    CompactionSummary,
    PinnedFact,
)
from code_rook.core.compact.protocol import (
    estimate_messages_tokens,
    evaluate_summary_quality,
    parse_summary,
    split_recent_window,
    summary_message,
    validate_tool_protocol,
)
from code_rook.core.events.bus import EventBus

if TYPE_CHECKING:
    from code_rook.core.context import ExecutionContext
    from code_rook.core.llm.base import LLMProvider
    from code_rook.core.session.store import SessionStore

logger = logging.getLogger(__name__)

_COMPACT_PROMPT = """\
Compress the OLD portion of an AI coding-agent conversation into one JSON object.
The recent conversation is retained verbatim and is not included below.
If the input contains a previous [CODEROOK_COMPACTION_V2] summary, merge it with newer facts.

Return JSON only with this exact shape:
{
  "goal": "current user goal",
  "completed": ["verified completed work"],
  "constraints": ["user requirements and non-negotiable project rules"],
  "decisions": ["architecture decisions and rationale"],
  "files": [{"path": "exact/path.py", "state": "created/modified/current role"}],
  "todos": ["ordered unfinished work"],
  "errors": ["unresolved errors or important resolved failure causes"],
  "critical_data": ["IDs, commands, config values, and exact facts needed later"]
  "pinned_facts": [
    {"id": "provided fact id", "text": "faithful fact text", "source_event_seqs": [1]}
  ]
}

Preserve exact file paths and user constraints. Do not invent completion, files, or errors.
Copy every provided pinned fact with the same id and source_event_seqs; omission is invalid.
Omit reasoning and failed attempts unless their cause changes the next action.
"""


# 返回当前 UTC 时间的简短时间戳字符串用于摘要文件名
def _ts_compact() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CompactionResult:
    summary: CompactionSummary
    summary_text: str
    original_token_estimate: int
    summary_tokens: int
    retained_tokens: int
    retained_messages: int
    compacted_tokens: int
    quality: CompactionQuality
    messages: list[dict[str, Any]]
    summary_path: str = ""
    strategy: str = "structured"
    pinned_fact_count: int = 0
    pinned_fact_retained: int = 0
    deduplicated_reads: int = 0


class Compactor:
    # 初始化压缩器并配置最近原文窗口的保留比例
    def __init__(
        self,
        bus: EventBus,
        session_dir: Path,
        session_id: str,
        *,
        store: SessionStore | None = None,
        retain_ratio: float = 0.25,
        strategy: str = "adaptive_evidence",
    ) -> None:
        self._bus = bus
        self._session_dir = session_dir
        self._session_id = session_id
        self._store = store
        self._retain_ratio = retain_ratio
        if strategy not in {"truncate", "structured", "adaptive_evidence"}:
            raise ValueError(f"unknown compaction strategy: {strategy}")
        self._strategy = strategy

    # 增量压缩旧窗口并在质量检查通过后原子替换执行上下文
    async def compact(
        self,
        context: ExecutionContext,
        provider: LLMProvider,
        focus: str = "",
        *,
        trigger: str = "auto",
    ) -> CompactionResult | None:
        force = trigger == "overflow"
        result = await self.compact_messages(
            context.messages,
            provider,
            focus=focus,
            retain_ratio=0.0 if force else None,
            force=force,
        )
        if result is None:
            return None

        await self.commit(result, run_id=context.run_id, trigger=trigger)
        context.messages = result.messages
        logger.info(
            "context compacted session=%s run=%s original≈%d compacted≈%d retained=%d quality=%.2f",
            self._session_id,
            context.run_id,
            result.original_token_estimate,
            result.compacted_tokens,
            result.retained_tokens,
            result.quality.score,
        )
        return result

    # 持久化压缩结果并按需发布可观测事件
    async def commit(
        self,
        result: CompactionResult,
        *,
        run_id: str,
        trigger: str,
        publish: bool = True,
    ) -> None:
        result.summary_path = self._write_summary(result)
        shadowed: list[int] = []
        replacements: list[int] = []
        if self._store is not None and self._session_id:
            shadowed, replacements = self._store.append_compaction(
                self._session_id,
                result.messages,
                run_id=run_id,
                summary=result.summary_text,
                trigger=trigger,
                original_tokens=result.original_token_estimate,
                compacted_tokens=result.compacted_tokens,
                pinned_fact_count=result.pinned_fact_count,
            )
        if publish:
            if shadowed:
                await self._publish_projection_events(
                    run_id,
                    result,
                    trigger,
                    shadowed,
                    replacements,
                )
            await self._publish_event(run_id, result, trigger)

    # 压缩旧消息并返回结构化摘要与完整最近窗口，失败时不修改输入
    async def compact_messages(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        focus: str = "",
        *,
        retain_ratio: float | None = None,
        force: bool = False,
        strategy: str | None = None,
    ) -> CompactionResult | None:
        from code_rook.core.events.bus import EventBus as _Bus

        protocol_valid, protocol_errors = validate_tool_protocol(messages)
        if not protocol_valid:
            logger.warning("compactor: invalid tool protocol: %s", "; ".join(protocol_errors))
            return None

        selected_strategy = strategy or self._strategy
        ratio = self._retain_ratio if retain_ratio is None else retain_ratio
        older: list[dict[str, Any]]
        recent: list[dict[str, Any]]
        if ratio <= 0:
            older, recent = list(messages), []
        else:
            older, recent = split_recent_window(messages, ratio)
        if not older:
            logger.info("compactor: no old window available; skipping")
            return None

        original_estimate = estimate_messages_tokens(messages)
        pinned_facts = (
            self._extract_pinned_facts() if selected_strategy == "adaptive_evidence" else []
        )
        prepared_older, deduplicated_reads = (
            _deduplicate_tool_results(older)
            if selected_strategy == "adaptive_evidence"
            else (older, 0)
        )
        if selected_strategy == "truncate":
            return self._truncate_result(
                messages,
                recent,
                original_estimate=original_estimate,
                force=force,
            )
        history_text = _messages_to_text(prepared_older)
        prompt = _COMPACT_PROMPT
        if pinned_facts:
            prompt += "\n\nPinned facts that must be copied exactly:\n" + json.dumps(
                [fact.model_dump(mode="json") for fact in pinned_facts],
                ensure_ascii=False,
                sort_keys=True,
            )
        if focus.strip():
            prompt += f"\nFocus requested by the user/runtime: {focus.strip()}"
        compress_request: list[dict[str, object]] = [
            {"role": "user", "content": f"{prompt}\n\n--- OLD WINDOW ---\n{history_text}"}
        ]

        try:
            response = await provider.chat(
                messages=compress_request,
                tool_schemas=[],
                bus=_Bus(),
                run_id="compact",
                step=0,
                system="Return a faithful structured JSON handoff summary.",
            )
        except Exception:
            logger.exception("compactor: LLM call failed, skipping compaction")
            return None

        summary = parse_summary(response.text)
        if summary is None:
            logger.warning("compactor: LLM returned an invalid structured summary")
            return None
        quality = evaluate_summary_quality(summary, history_text)
        missing_facts = _missing_pinned_facts(summary, pinned_facts)
        if missing_facts:
            quality = CompactionQuality(
                passed=False,
                score=0.0,
                checks={**quality.checks, "pinned_facts": False},
                missing=[*quality.missing, *missing_facts],
            )
        if not quality.passed:
            logger.warning(
                "compactor: quality gate failed score=%.2f missing=%s",
                quality.score,
                quality.missing,
            )
            return None

        rendered = summary_message(summary)
        output_messages = [
            {"role": "user", "content": rendered},
            {
                "role": "assistant",
                "content": "Compaction restored; continuing with recent context.",
            },
            *recent,
        ]
        output_valid, output_errors = validate_tool_protocol(output_messages)
        if not output_valid:
            logger.warning("compactor: output protocol invalid: %s", "; ".join(output_errors))
            return None

        summary_tokens = (
            response.usage.output_tokens if response.usage else max(1, len(rendered) // 4)
        )
        retained_tokens = estimate_messages_tokens(recent)
        compacted_tokens = estimate_messages_tokens(output_messages)
        if not force and compacted_tokens >= original_estimate:
            logger.info(
                "compactor: result not beneficial original≈%d compacted≈%d",
                original_estimate,
                compacted_tokens,
            )
            return None
        return CompactionResult(
            summary=summary,
            summary_text=rendered,
            original_token_estimate=original_estimate,
            summary_tokens=summary_tokens,
            retained_tokens=retained_tokens,
            retained_messages=len(recent),
            compacted_tokens=compacted_tokens,
            quality=quality,
            messages=output_messages,
            strategy=selected_strategy,
            pinned_fact_count=len(pinned_facts),
            pinned_fact_retained=len(pinned_facts),
            deduplicated_reads=deduplicated_reads,
        )

    # 从事实日志提取目标、策略、未决审批、失败工具和修改结果等不可丢失事实
    def _extract_pinned_facts(self) -> list[PinnedFact]:
        if self._store is None or not self._session_id:
            return []
        events = self._store.read_session_events(self._session_id)
        resolved_permissions = {
            str(event.payload.get("tool_use_id", ""))
            for event in events
            if event.type == "permission.resolved"
        }
        selected: list[PinnedFact] = []
        latest_input = next(
            (event for event in reversed(events) if event.type == "input.admitted"),
            None,
        )
        if latest_input is not None:
            selected.append(
                PinnedFact(
                    id="current_goal",
                    text=_event_fact_text(latest_input.payload),
                    source_event_seqs=[latest_input.seq],
                )
            )
        latest_profile = next(
            (event for event in reversed(events) if event.type == "task.profiled"),
            None,
        )
        if latest_profile is not None:
            selected.append(
                PinnedFact(
                    id="task_profile",
                    text=_event_fact_text(latest_profile.payload),
                    source_event_seqs=[latest_profile.seq],
                )
            )
        for event in events:
            tool_use_id = str(event.payload.get("tool_use_id", ""))
            if event.type == "permission.requested" and tool_use_id not in resolved_permissions:
                selected.append(
                    PinnedFact(
                        id=f"pending_permission_{event.seq}",
                        text=_event_fact_text(event.payload),
                        source_event_seqs=[event.seq],
                    )
                )
            elif event.type == "tool.call_failed":
                selected.append(
                    PinnedFact(
                        id=f"failed_tool_{event.seq}",
                        text=_event_fact_text(event.payload),
                        source_event_seqs=[event.seq],
                    )
                )
        latest_plan = next(
            (event for event in reversed(events) if event.type == "plan.updated"),
            None,
        )
        if latest_plan is not None:
            selected.append(
                PinnedFact(
                    id="active_plan",
                    text=_event_fact_text(latest_plan.payload),
                    source_event_seqs=[latest_plan.seq],
                )
            )
        latest_verification = next(
            (
                event
                for event in reversed(events)
                if event.type in {"verification.completed", "verification.failed"}
            ),
            None,
        )
        if latest_verification is not None:
            selected.append(
                PinnedFact(
                    id="latest_verification",
                    text=_event_fact_text(latest_verification.payload),
                    source_event_seqs=[latest_verification.seq],
                )
            )
        return selected

    # 发布 append-only 压缩事务的开始、摘要和提交事件供 TUI 与回放消费
    async def _publish_projection_events(
        self,
        run_id: str,
        result: CompactionResult,
        trigger: str,
        shadowed: list[int],
        replacements: list[int],
    ) -> None:
        await self._bus.publish(
            ContextCompactionStartedEvent(
                session_id=self._session_id,
                run_id=run_id,
                shadow_start_seq=min(shadowed),
                shadow_end_seq=max(shadowed),
                trigger=trigger,
                ts=_now(),
            )
        )
        await self._bus.publish(
            ContextCompactionSummaryEvent(
                session_id=self._session_id,
                run_id=run_id,
                summary=result.summary_text,
                source_event_seqs=shadowed,
                pinned_fact_count=result.pinned_fact_count,
                ts=_now(),
            )
        )
        await self._bus.publish(
            ContextCompactionCommittedEvent(
                session_id=self._session_id,
                run_id=run_id,
                shadowed_event_seqs=shadowed,
                replacement_event_seqs=replacements,
                original_tokens=result.original_token_estimate,
                compacted_tokens=result.compacted_tokens,
                ts=_now(),
            )
        )

    # 生成不依赖模型的头尾截断对照结果并保持完整工具调用原子组
    def _truncate_result(
        self,
        messages: list[dict[str, Any]],
        recent: list[dict[str, Any]],
        *,
        original_estimate: int,
        force: bool,
    ) -> CompactionResult | None:
        first_user = next(
            (message for message in messages if message.get("role") == "user"),
            None,
        )
        output_messages = ([first_user] if first_user is not None else []) + recent
        valid, _errors = validate_tool_protocol(output_messages)
        if not valid:
            return None
        compacted_tokens = estimate_messages_tokens(output_messages)
        if not force and compacted_tokens >= original_estimate:
            return None
        goal = str((first_user or {}).get("content", "continued task"))[:2_000]
        summary = CompactionSummary(goal=goal or "continued task")
        quality = CompactionQuality(
            passed=True,
            score=1.0,
            checks={"tool_protocol": True},
        )
        return CompactionResult(
            summary=summary,
            summary_text=goal,
            original_token_estimate=original_estimate,
            summary_tokens=max(1, len(goal) // 4),
            retained_tokens=estimate_messages_tokens(recent),
            retained_messages=len(recent),
            compacted_tokens=compacted_tokens,
            quality=quality,
            messages=output_messages,
            strategy="truncate",
        )

    # 发布包含触发原因、保留窗口和质量分的类型化压缩事件
    async def _publish_event(
        self,
        run_id: str,
        result: CompactionResult,
        trigger: str,
    ) -> None:
        await self._bus.publish(
            ContextCompactedEvent(
                session_id=self._session_id,
                run_id=run_id,
                original_tokens=result.original_token_estimate,
                summary_tokens=result.summary_tokens,
                retained_tokens=result.retained_tokens,
                retained_messages=result.retained_messages,
                compacted_tokens=result.compacted_tokens,
                quality_score=result.quality.score,
                trigger=trigger,
                summary_path=result.summary_path,
                strategy=result.strategy,
                pinned_fact_count=result.pinned_fact_count,
                pinned_fact_retained=result.pinned_fact_retained,
                deduplicated_reads=result.deduplicated_reads,
                ts=_now(),
            )
        )

    # 将结构化摘要和质量元数据写入 session 目录并返回路径
    def _write_summary(self, result: CompactionResult) -> str:
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            path = self._session_dir / f"summary_{_ts_compact()}.md"
            metadata = (
                f"<!-- quality={result.quality.score:.2f} "
                f"retained_messages={result.retained_messages} -->\n"
            )
            path.write_text(metadata + result.summary.to_markdown(), encoding="utf-8")
            return str(path)
        except Exception:
            logger.exception("compactor: failed to write summary file")
            return ""


# 将消息列表序列化为可供压缩模型阅读的稳定纯文本
def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            blocks: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(str(block.get("text", "")))
                elif btype == "tool_use":
                    blocks.append(
                        f"<tool_call name={block.get('name')} id={block.get('id')}>\n"
                        f"{block.get('input', {})}\n</tool_call>"
                    )
                elif btype == "tool_result":
                    blocks.append(
                        f"<tool_result id={block.get('tool_use_id')}>\n"
                        f"{block.get('content', '')}\n</tool_result>"
                    )
            parts.append(f"[{role}]\n" + "\n".join(blocks))
    return "\n\n".join(parts)


# 将事件载荷稳定压缩为可供摘要模型逐字保留的事实文本
def _event_fact_text(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )[:2_000]


# 校验模型摘要完整保留每个固定事实的 ID 和来源事件序号
def _missing_pinned_facts(
    summary: CompactionSummary,
    required: list[PinnedFact],
) -> list[str]:
    actual = {fact.id: (fact.text, tuple(fact.source_event_seqs)) for fact in summary.pinned_facts}
    return [
        f"pinned_fact:{fact.id}"
        for fact in required
        if actual.get(fact.id) != (fact.text, tuple(fact.source_event_seqs))
    ]


# 用内容哈希折叠旧窗口内重复工具结果，完整原文仍保留在事实日志
def _deduplicate_tool_results(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    copied = json.loads(json.dumps(messages, ensure_ascii=False))
    seen: set[str] = set()
    removed = 0
    for message in reversed(copied):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("content", "")
            if not isinstance(raw, str) or len(raw) < 128:
                continue
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if digest in seen:
                block["content"] = (
                    "[duplicate tool result omitted; identical later result retained; "
                    f"sha256={digest}]"
                )
                removed += 1
            else:
                seen.add(digest)
    return copied, removed

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from code_rook.core.bus.events import ContextCompactedEvent
from code_rook.core.compact.models import CompactionQuality, CompactionSummary
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
}

Preserve exact file paths and user constraints. Do not invent completion, files, or errors.
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
    ) -> None:
        self._bus = bus
        self._session_dir = session_dir
        self._session_id = session_id
        self._store = store
        self._retain_ratio = retain_ratio

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

        context.messages = result.messages
        await self.commit(result, run_id=context.run_id, trigger=trigger)
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
        if self._store is not None and self._session_id:
            self._store.write_compacted(self._session_id, result.messages)
        if publish:
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
    ) -> CompactionResult | None:
        from code_rook.core.events.bus import EventBus as _Bus

        protocol_valid, protocol_errors = validate_tool_protocol(messages)
        if not protocol_valid:
            logger.warning("compactor: invalid tool protocol: %s", "; ".join(protocol_errors))
            return None

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
        history_text = _messages_to_text(older)
        prompt = _COMPACT_PROMPT
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

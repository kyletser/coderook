from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from code_rook.core.events.bus import EventBus

if TYPE_CHECKING:
    from code_rook.core.llm.base import LLMProvider

TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000
TOOL_RESULT_SUMMARIZE_THRESHOLD = 20_000
_DISTILLED_MARKER = "[CodeRook distilled tool output"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolResultBudgetStats:
    distilled: int = 0
    truncated: int = 0


# 将超长文本按总保留量截成头尾两段并附加可追溯标记
def _truncate_text(text: str, keep: int) -> str:
    head_size = max(1, keep // 2)
    tail_size = max(1, keep - head_size)
    omitted = max(0, len(text) - head_size - tail_size)
    return (
        text[:head_size]
        + f"\n[... {omitted} chars omitted. Full output in run events ...]\n"
        + text[-tail_size:]
    )


# 对消息中的中大型 tool_result 做确定性头尾截断并返回新列表
def truncate_tool_results(
    messages: list[dict[str, Any]],
    limit: int = TOOL_RESULT_LIMIT,
    keep: int = TOOL_RESULT_KEEP,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            result.append(msg)
            continue
        new_blocks: list[Any] = []
        for raw_block in content:
            block = raw_block
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = block.get("content")
                if isinstance(text, str) and len(text) > limit:
                    block = {**block, "content": _truncate_text(text, keep)}
            new_blocks.append(block)
        result.append({**msg, "content": new_blocks})
    return result


# 使用静默 LLM 调用把超大工具输出蒸馏成决策相关事实并在失败时回退截断
async def distill_tool_results(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    *,
    threshold: int = TOOL_RESULT_SUMMARIZE_THRESHOLD,
    fallback_keep: int = TOOL_RESULT_KEEP,
) -> tuple[list[dict[str, Any]], ToolResultBudgetStats]:
    output: list[dict[str, Any]] = []
    distilled = 0
    truncated = 0
    for msg in messages:
        content = msg.get("content")
        if msg.get("role") != "user" or not isinstance(content, list):
            output.append(msg)
            continue
        new_blocks: list[Any] = []
        for raw_block in content:
            block = raw_block
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = block.get("content")
                if (
                    isinstance(text, str)
                    and len(text) > threshold
                    and not text.startswith(_DISTILLED_MARKER)
                ):
                    compacted = await _distill_one(
                        provider,
                        str(block.get("tool_use_id", "")),
                        text,
                    )
                    if compacted is None:
                        compacted = _truncate_text(text, fallback_keep)
                        truncated += 1
                    else:
                        distilled += 1
                    block = {**block, "content": compacted}
            new_blocks.append(block)
        output.append({**msg, "content": new_blocks})
    return output, ToolResultBudgetStats(distilled=distilled, truncated=truncated)


# 蒸馏单个工具输出并返回带来源标记的摘要
async def _distill_one(
    provider: LLMProvider,
    tool_use_id: str,
    content: str,
) -> str | None:
    bounded = content if len(content) <= 100_000 else _truncate_text(content, 80_000)
    request: list[dict[str, object]] = [{
        "role": "user",
        "content": (
            "Summarize this tool output for a coding agent. Preserve exact errors, file paths, "
            "test counts, commands, and actionable findings. Omit repetitive lines.\n\n" + bounded
        ),
    }]
    try:
        response = await provider.chat(
            messages=request,
            tool_schemas=[],
            bus=EventBus(),
            run_id="tool-distill",
            step=0,
            system="Return a concise, factual tool-output summary.",
        )
    except Exception:
        logger.exception("tool output distillation failed tool_use_id=%s", tool_use_id)
        return None
    summary = response.text.strip()
    if not summary:
        return None
    return (
        f"{_DISTILLED_MARKER} id={tool_use_id} original_chars={len(content)}]\n{summary}"
    )

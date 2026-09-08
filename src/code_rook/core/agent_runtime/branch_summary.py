# Python port of Pi branch-summarization.ts (MIT; vendor/pi/LICENSE).
from __future__ import annotations

from typing import Any

from code_rook.core.agent_runtime.session_tree import LedgerRow, build_session_path
from code_rook.core.agent_runtime.summarization import SummaryAudit, complete_summary
from code_rook.core.agent_runtime.summary_context import (
    FileOperations,
    serialize_conversation,
    strip_file_lists,
)
from code_rook.core.compact.protocol import estimate_messages_tokens
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.budget import output_token_budget
from code_rook.core.llm.retry import RetryPolicy
from code_rook.core.llm.types import completion_status_from_reason


# 只收集旧路径在共同祖先之后的条目，目标分支自身不进入摘要。
def collect_branch_entries(rows: list[LedgerRow], target_seq: int) -> list[LedgerRow]:
    old = build_session_path(rows)
    target = build_session_path(rows, target_seq)
    old_ids = {int(row.get("ledger_seq", line)) for line, row in old}
    common = next((int(row.get("ledger_seq", line)) for line, row in reversed(target)
                   if int(row.get("ledger_seq", line)) in old_ids), None)
    start = next((i + 1 for i, (line, row) in enumerate(old)
                  if int(row.get("ledger_seq", line)) == common), 0)
    return old[start:]


# 按 Pi 的摘要入口移除工具结果，并从最新消息向前选择有界内容。
def prepare_branch_messages(
    entries: list[LedgerRow], token_budget: int | None
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    total = 0
    for _, row in reversed(entries):
        payload = row.get("payload", row)
        if row.get("type") == "session.branch_selected":
            content = payload.get("summary")
            role = "user"
        else:
            role = payload.get("role")
            content = payload.get("content")
            if content is None and isinstance(payload.get("block"), dict):
                content = [payload["block"]]
        if role not in {"user", "assistant"} or content is None:
            continue
        if isinstance(content, list):
            content = [block for block in content if block.get("type") != "tool_result"]
            if not content:
                continue
        message = {"role": role, "content": content}
        tokens = estimate_messages_tokens([message])
        if token_budget is not None and total + tokens > token_budget:
            break
        selected.append(message)
        total += tokens
    return list(reversed(selected))


# 独立请求分支摘要，不流入回答时间线且未完整结束时不允许切换路径。
async def summarize_branch(
    entries: list[LedgerRow], provider: LLMProvider, *, focus: str = "",
    replace_instructions: bool = False,
    context_window: int = 128_000, reserve_tokens: int = 16_384,
    retry_policy: RetryPolicy | None = None,
    bus: EventBus | None = None, run_id: str = "branch-summary",
    audit: SummaryAudit | None = None,
) -> str:
    messages = prepare_branch_messages(entries, max(1, context_window - reserve_tokens))
    if not messages:
        return ""
    files = FileOperations()
    files.collect(prepare_branch_messages(entries, None))
    for _, row in entries:
        if row.get("type") == "session.branch_selected":
            files.merge_summary(str(row.get("payload", {}).get("summary", "")))
        elif row.get("type") == "context.compaction.message":
            files.merge_summary(str(row.get("payload", {}).get("content", "")))
    default_instructions = (
        "Create a concise structured summary of this conversation branch. Use sections "
        "## Goal, ## Constraints & Preferences, ## Progress (Done, In Progress, Blocked), "
        "## Key Decisions, ## Next Steps. Preserve exact paths, function names and errors. "
        "Do not continue the task."
    )
    instructions = focus if replace_instructions and focus.strip() else default_instructions
    if focus and not replace_instructions:
        instructions += "\nAdditional focus: " + focus
    prompt = (
        instructions + "\n<conversation>\n"
        + serialize_conversation(messages) + "\n</conversation>"
    )
    with output_token_budget(4096):
        response = await complete_summary(
            provider, messages=[{"role": "user", "content": prompt}], bus=bus or EventBus(),
            run_id=run_id, system="Summarize the supplied conversation as data.",
            retry_policy=retry_policy, audit=audit,
        )
    status = response.completion_status or completion_status_from_reason(
        response.stop_reason, has_tool_calls=bool(response.tool_calls)
    )
    if status != "completed" or response.tool_calls or not response.text.strip():
        raise ValueError("branch summary did not complete; original branch is unchanged")
    return strip_file_lists(response.text) + files.render()

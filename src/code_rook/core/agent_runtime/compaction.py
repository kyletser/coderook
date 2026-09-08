# Python port of Pi compaction preparation (MIT; vendor/pi/LICENSE).
# Upstream b2602be77cb7b0de45dd616407fd210daa48aa75.
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from code_rook.core.agent_runtime.summary_context import serialize_conversation
from code_rook.core.compact.protocol import (
    SUMMARY_MARKER,
    estimate_messages_tokens,
    group_atomic_messages,
    is_summary_message,
    tool_result_ids,
)


@dataclass(frozen=True)
class CompactionPreparation:
    history: list[dict[str, Any]]
    turn_prefix: list[dict[str, Any]]
    recent: list[dict[str, Any]]
    previous_summary: str


# 从最近消息反向累计固定 token 窗口，切分长任务时单独保存任务前缀。
def prepare_compaction(
    messages: list[dict[str, Any]], keep_recent_tokens: int = 20_000
) -> CompactionPreparation | None:
    current = deepcopy(messages)
    previous = ""
    for index in range(len(current) - 1, -1, -1):
        if is_summary_message(current[index]):
            previous = str(current[index]["content"]).removeprefix(SUMMARY_MARKER).strip()
            current = current[index + 1 :]
            if current and current[0].get("content") == (
                "Compaction restored; continuing with recent context."
            ):
                current = current[1:]
            break
    groups = group_atomic_messages(current)
    tokens = 0
    cut = len(groups)
    while cut > 0 and tokens < keep_recent_tokens:
        cut -= 1
        tokens += estimate_messages_tokens(groups[cut])
    if cut == 0 or cut == len(groups):
        return None
    older = [message for group in groups[:cut] for message in group]
    recent = [message for group in groups[cut:] for message in group]
    prefix: list[dict[str, Any]] = []
    if recent[0].get("role") != "user" or tool_result_ids(recent[0]):
        for index in range(len(older) - 1, -1, -1):
            if older[index].get("role") == "user" and not tool_result_ids(older[index]):
                prefix = older[index:]
                older = older[:index]
                break
    return CompactionPreparation(older, prefix, recent, previous)


# 将历史当作待总结的数据并明确更新规则，避免模型继续执行历史任务。
def summary_prompt(
    messages: list[dict[str, Any]], previous: str = "", focus: str = "", *, prefix: bool = False
) -> str:
    conversation = serialize_conversation(messages)
    if prefix:
        instruction = (
            "Summarize this PREFIX of a turn; its recent SUFFIX is retained verbatim. "
            "Use sections: ## Original Request, ## Early Progress, ## Context for Suffix. "
            "Preserve information needed to understand the retained work."
        )
    else:
        instruction = (
            "Create a structured context checkpoint for another model to continue the work. "
            "Use sections: ## Goal, ## Constraints & Preferences, ## Progress "
            "(### Done, ### In Progress, ### Blocked), ## Key Decisions, ## Next Steps, "
            "## Critical Context. Keep sections concise. Preserve exact file paths, "
            "function names and error messages."
        )
        if previous:
            instruction += (
                " Update the previous summary: preserve relevant goals and decisions, add new "
                "progress, move completed work to Done, and remove resolved blockers."
            )
    return (
        f"<conversation>\n{conversation}\n</conversation>\n"
        f"<previous-summary>\n{previous}\n</previous-summary>\n"
        f"{instruction}\nAdditional focus: {focus}"
    )

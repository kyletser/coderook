from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from code_rook.core.compact.models import CompactionQuality, CompactionSummary

SUMMARY_MARKER = "[CODEROOK_COMPACTION_V2]"
_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z]:[\\/])?[\w.-]+(?:[\\/][\w.@+() -]+)+\.[A-Za-z0-9]{1,10}"
)
_CONSTRAINT_RE = re.compile(
    r"(?i)(必须|不能|不要|始终|只允许|要求|约束|must|never|do not|required|only)"
)
_TODO_RE = re.compile(r"(?i)(todo|待办|未完成|下一步|remaining|still need|需要继续)")
_ERROR_RE = re.compile(r"(?i)(error|failed|failure|exception|报错|失败|异常)")


# 使用字符数近似估算消息 token，保证无 tokenizer 时仍可确定性运行
def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return max(1, sum(len(str(message.get("content", ""))) for message in messages) // 4)


# 返回消息内声明的 tool_use ID 集合
def tool_use_ids(message: dict[str, Any]) -> set[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return set()
    return {
        str(block.get("id"))
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
    }


# 返回消息内响应的 tool_result ID 集合
def tool_result_ids(message: dict[str, Any]) -> set[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return set()
    return {
        str(block.get("tool_use_id"))
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "tool_result"
        and block.get("tool_use_id")
    }


# 验证所有工具结果均有调用且所有工具调用均已闭环
def validate_tool_protocol(messages: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    pending: set[str] = set()
    errors: list[str] = []
    for index, message in enumerate(messages):
        uses = tool_use_ids(message)
        results = tool_result_ids(message)
        duplicate = pending & uses
        if duplicate:
            errors.append(f"message {index}: duplicate tool_use ids {sorted(duplicate)}")
        pending.update(uses)
        orphaned = results - pending
        if orphaned:
            errors.append(f"message {index}: orphan tool_result ids {sorted(orphaned)}")
        pending.difference_update(results)
    if pending:
        errors.append(f"unresolved tool_use ids {sorted(pending)}")
    return not errors, errors


# 将消息按普通消息或完整工具调用闭环组成不可拆分的原子组
def group_atomic_messages(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(messages):
        current = messages[index]
        uses = tool_use_ids(current)
        group = [current]
        if uses:
            pending = set(uses)
            cursor = index + 1
            while cursor < len(messages) and pending:
                candidate = messages[cursor]
                group.append(candidate)
                pending.difference_update(tool_result_ids(candidate))
                cursor += 1
            index = cursor
        else:
            index += 1
        groups.append(group)
    return groups


# 按 token 比例保留最近原子消息组并返回旧历史与最近窗口
def split_recent_window(
    messages: list[dict[str, Any]],
    retain_ratio: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups = group_atomic_messages(messages)
    if len(groups) <= 1:
        return [], list(messages)
    target = max(1, int(estimate_messages_tokens(messages) * retain_ratio))
    retained_groups: list[list[dict[str, Any]]] = []
    retained_tokens = 0
    for group in reversed(groups):
        if retained_groups and retained_tokens >= target:
            break
        retained_groups.append(group)
        retained_tokens += estimate_messages_tokens(group)
    retained_groups.reverse()
    split_at = len(groups) - len(retained_groups)
    older = [message for group in groups[:split_at] for message in group]
    recent = [message for group in retained_groups for message in group]
    return older, recent


# 从模型文本中提取 JSON 并校验为结构化压缩摘要
def parse_summary(text: str) -> CompactionSummary | None:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
        return CompactionSummary.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError):
        return None


# 将结构化摘要包装成可识别的持久化上下文消息
def summary_message(summary: CompactionSummary) -> str:
    return f"{SUMMARY_MARKER}\n{summary.to_markdown()}"


# 判断消息是否为上一轮压缩产生的结构化摘要
def is_summary_message(message: dict[str, Any]) -> bool:
    return message.get("role") == "user" and str(message.get("content", "")).startswith(
        SUMMARY_MARKER
    )


# 根据源历史中的目标、约束、待办、错误和路径信号评估摘要完整度
def evaluate_summary_quality(
    summary: CompactionSummary,
    source_text: str,
) -> CompactionQuality:
    signal_text = re.sub(
        r"(?im)^## (?:Constraints|TODO|Errors)\s*$",
        "",
        source_text,
    )
    source_paths = set(_PATH_RE.findall(signal_text))
    summary_paths = {item.path for item in summary.files}
    checks = {
        "goal": bool(summary.goal.strip()),
        "constraints": not _CONSTRAINT_RE.search(signal_text) or bool(summary.constraints),
        "todos": not _TODO_RE.search(signal_text) or bool(summary.todos),
        "errors": not _ERROR_RE.search(signal_text) or bool(summary.errors),
        "files": not source_paths or bool(source_paths & summary_paths),
    }
    missing = [name for name, passed in checks.items() if not passed]
    score = sum(checks.values()) / len(checks)
    return CompactionQuality(
        passed=not missing,
        score=score,
        checks=checks,
        missing=missing,
    )

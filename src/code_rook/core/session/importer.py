from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

SessionImportFormat = Literal["coderook-json", "pi-jsonl"]


@dataclass(frozen=True)
class ImportedSession:
    title: str
    messages: list[dict[str, Any]]
    notes: str
    source_format: SessionImportFormat


# 将 Pi 的内容块转换成 CodeRook 使用的 Anthropic 风格内容块。
def _pi_content_block(block: object) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        return None
    block_type = block.get("type")
    if block_type == "text" and isinstance(block.get("text"), str):
        return {"type": "text", "text": block["text"]}
    if block_type == "thinking" and isinstance(block.get("thinking"), str):
        return {"type": "thinking", "thinking": block["thinking"]}
    if block_type == "image" and isinstance(block.get("data"), str):
        mime_type = block.get("mimeType")
        if isinstance(mime_type, str) and mime_type:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": block["data"],
                },
            }
    if block_type == "toolCall":
        identifier = block.get("id")
        name = block.get("name")
        arguments = block.get("arguments", {})
        if isinstance(identifier, str) and isinstance(name, str) and isinstance(arguments, dict):
            return {
                "type": "tool_use",
                "id": identifier,
                "name": name,
                "input": arguments,
            }
    return None


# 将 Pi 的用户或助手消息转换为 CodeRook 模型历史消息。
def _pi_regular_message(message: dict[str, Any]) -> dict[str, Any] | None:
    role = message.get("role")
    if role not in {"user", "assistant"}:
        return None
    content = message.get("content", "")
    if isinstance(content, str):
        return {"role": role, "content": content}
    if not isinstance(content, list):
        return None
    blocks = [converted for item in content if (converted := _pi_content_block(item))]
    return {"role": role, "content": blocks}


# 将 Pi 独立的 toolResult 消息合并为 CodeRook 的 user/tool_result 消息。
def _pi_tool_result(message: dict[str, Any]) -> dict[str, Any] | None:
    identifier = message.get("toolCallId")
    if not isinstance(identifier, str) or not identifier:
        return None
    raw_content = message.get("content", [])
    converted: list[dict[str, Any]] = []
    if isinstance(raw_content, str):
        result_content: object = raw_content
    elif isinstance(raw_content, list):
        for item in raw_content:
            converted_block = _pi_content_block(item)
            if converted_block is not None:
                converted.append(converted_block)
        text_blocks = [
            str(block.get("text", ""))
            for block in converted
            if block.get("type") == "text"
        ]
        result_content = "\n".join(text_blocks) if len(text_blocks) == len(converted) else converted
    else:
        return None
    result_block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": identifier,
        "content": result_content,
    }
    if message.get("isError") is True:
        result_block["is_error"] = True
    return {"role": "user", "content": [result_block]}


# 沿 Pi 会话最后一个叶节点回溯，得到当前活动分支而非全部废弃分支。
def _pi_active_path(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = [row for row in rows if row.get("type") != "session"]
    by_id = {
        str(row["id"]): row
        for row in entries
        if isinstance(row.get("id"), str) and row["id"]
    }
    if not entries:
        return []
    current: dict[str, Any] | None = entries[-1]
    path: list[dict[str, Any]] = []
    seen: set[str] = set()
    while current is not None:
        identifier = current.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                raise ValueError("Pi session contains a parent cycle")
            seen.add(identifier)
        path.append(current)
        parent = current.get("parentId")
        current = by_id.get(parent) if isinstance(parent, str) else None
    path.reverse()
    return path


# 按 Pi 的最新 compaction 语义投影真正会送入模型的活动上下文条目。
def _pi_context_path(path: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact_index = next(
        (
            index
            for index in range(len(path) - 1, -1, -1)
            if path[index].get("type") == "compaction"
        ),
        None,
    )
    if compact_index is None:
        return path
    compaction = path[compact_index]
    first_kept = compaction.get("firstKeptEntryId")
    kept_index = next(
        (
            index
            for index, entry in enumerate(path[:compact_index])
            if entry.get("id") == first_kept
        ),
        compact_index,
    )
    return [compaction, *path[kept_index:compact_index], *path[compact_index + 1 :]]


# 解析 CodeRook 自有 JSON 导出并规范化可继续对话的消息。
def _parse_coderook_json(payload: object) -> ImportedSession:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported CodeRook session export")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise ValueError("CodeRook session export has no message list")
    messages: list[dict[str, Any]] = []
    for raw in raw_messages:
        if not isinstance(raw, dict) or raw.get("role") not in {"user", "assistant"}:
            raise ValueError("CodeRook session export contains an invalid message")
        content = raw.get("content", "")
        if not isinstance(content, (str, list)):
            raise ValueError("CodeRook session export contains invalid message content")
        messages.append({"role": str(raw["role"]), "content": content})
    raw_session = payload.get("session")
    title = str(raw_session.get("title", "")) if isinstance(raw_session, dict) else ""
    notes = payload.get("notes", "")
    if not isinstance(notes, str):
        raise ValueError("CodeRook session export contains invalid notes")
    return ImportedSession(title, messages, notes, "coderook-json")


# 解析 Pi JSONL 的活动分支并转换工具调用、结果、压缩和分支摘要。
def _parse_pi_jsonl(content: str) -> ImportedSession:
    rows: list[dict[str, Any]] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("Pi session JSONL contains invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError("Pi session JSONL entry must be an object")
        rows.append(row)
    if not rows or rows[0].get("type") != "session":
        raise ValueError("Pi session JSONL header is missing")

    messages: list[dict[str, Any]] = []
    active_path = _pi_active_path(rows)
    title = next(
        (
            str(row["name"])
            for row in reversed(active_path)
            if row.get("type") == "session_info" and isinstance(row.get("name"), str)
        ),
        "",
    )
    for row in _pi_context_path(active_path):
        entry_type = row.get("type")
        if entry_type == "message" and isinstance(row.get("message"), dict):
            message = row["message"]
            converted = (
                _pi_tool_result(message)
                if message.get("role") == "toolResult"
                else _pi_regular_message(message)
            )
            if converted is not None:
                messages.append(converted)
        elif entry_type in {"compaction", "branch_summary"} and isinstance(
            row.get("summary"), str
        ):
            messages.append({"role": "user", "content": str(row["summary"])})
        elif entry_type == "custom_message" and isinstance(row.get("content"), (str, list)):
            messages.append({"role": "user", "content": row["content"]})
    return ImportedSession(title, messages, "", "pi-jsonl")


# 自动识别 CodeRook JSON 或 Pi JSONL 会话导出并返回统一导入数据。
def import_session_content(content: str) -> ImportedSession:
    if not content.strip():
        raise ValueError("session import is empty")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return _parse_pi_jsonl(content)
    return _parse_coderook_json(payload)

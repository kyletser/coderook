from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any


# 按目标模型生成请求副本，转换历史图片、思考签名和工具 ID，保留原始历史。
def prepare_model_messages(
    messages: list[dict[str, Any]], *, supports_images: bool, wire_format: str = "",
    model: str = "", route_id: str = "",
) -> list[dict[str, Any]]:
    native = from_provider(messages)
    source = {"wire_format": wire_format, "model": model, "route_id": route_id}
    for message in native:
        if message["role"] != "assistant" or not isinstance(message.get("content"), list):
            continue
        assistant_blocks: list[dict[str, Any]] = []
        for block in message["content"]:
            origin = block.pop("_coderook_source", None)
            if block.get("type") not in {"thinking", "redacted_thinking"}:
                assistant_blocks.append(block)
                continue
            same_model = bool(model and wire_format and origin == source)
            if same_model:
                assistant_blocks.append(block)
            elif block.get("type") == "thinking" and str(block.get("thinking", "")).strip():
                assistant_blocks.append({"type": "text", "text": block["thinking"]})
        message["content"] = assistant_blocks
    for message in native:
        if supports_images:
            continue
        if message["role"] not in {"user", "toolResult"}:
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        placeholder = (
            "(tool image omitted: model does not support images)"
            if message["role"] == "toolResult"
            else "(image omitted: model does not support images)"
        )
        replaced: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") == "image":
                if not replaced or replaced[-1] != {"type": "text", "text": placeholder}:
                    replaced.append({"type": "text", "text": placeholder})
            else:
                replaced.append(block)
        message["content"] = replaced
    if wire_format == "anthropic_messages":
        ids: dict[str, str] = {}
        for message in native:
            if message["role"] != "assistant" or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if block.get("type") != "toolCall":
                    continue
                original = str(block["id"])
                if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", original):
                    continue
                # 保持合法字符和长度，并用摘要区分截断后相同的长 ID。
                prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", original)[:47]
                normalized = prefix + "_" + hashlib.sha256(original.encode()).hexdigest()[:16]
                ids[original] = normalized
                block["id"] = normalized
        for message in native:
            if message["role"] == "toolResult" and message["toolCallId"] in ids:
                message["toolCallId"] = ids[message["toolCallId"]]
    return to_provider(native)


# 将已有会话协议转换为 Pi 的独立消息模型，保持工具结果顺序及思考签名。
def from_provider(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for original in messages:
        message = deepcopy(original)
        blocks = message.get("content")
        if (
            message["role"] == "user"
            and isinstance(blocks, list)
            and blocks
            and any(block.get("type") == "tool_result" for block in blocks)
        ):
            user_blocks: list[dict[str, Any]] = []
            for block in blocks:
                if block.get("type") != "tool_result":
                    user_blocks.append(block)
                    continue
                if user_blocks:
                    converted.append({**message, "content": user_blocks})
                    user_blocks = []
                content = block.get("content", "")
                converted.append(
                    {
                        "role": "toolResult",
                        "toolCallId": block["tool_use_id"],
                        "content": [{"type": "text", "text": content}]
                        if isinstance(content, str)
                        else content,
                        "isError": block.get("is_error", False),
                    }
                )
            if user_blocks:
                converted.append({**message, "content": user_blocks})
        else:
            if isinstance(blocks, list):
                message["content"] = [
                    {
                        "type": "toolCall",
                        "id": block["id"],
                        "name": block["name"],
                        "arguments": block["input"],
                    }
                    if block.get("type") == "tool_use"
                    else block
                    for block in blocks
                ]
            converted.append(message)
    return converted


# 仅在 Provider 边界转换工具消息格式，内部循环不依赖 Anthropic 格式。
def to_provider(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for original in messages:
        message = deepcopy(original)
        if message.get("role") == "custom":
            converted.append({"role": "user", "content": message.get("content", [])})
            continue
        if message["role"] == "toolResult":
            content = message.get("content", [])
            if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
                content = content[0]["text"]
            block = {
                "type": "tool_result",
                "tool_use_id": message["toolCallId"],
                "content": content,
            }
            if message.get("isError"):
                block["is_error"] = True
            last = converted[-1] if converted else None
            if (
                last
                and last["role"] == "user"
                and isinstance(last["content"], list)
                and all(part.get("type") == "tool_result" for part in last["content"])
            ):
                last["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
        else:
            blocks = message.get("content", [])
            converted.append(
                {
                    "role": message["role"],
                    "content": [
                        {
                            "type": "tool_use",
                            "id": block["id"],
                            "name": block["name"],
                            "input": block["arguments"],
                        }
                        if block.get("type") == "toolCall"
                        else block
                        for block in blocks
                    ]
                    if isinstance(blocks, list)
                    else blocks,
                }
            )
    return converted

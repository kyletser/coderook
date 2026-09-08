# Python port of Pi compaction token estimates (MIT, vendor/pi/LICENSE).
import json
from typing import Any


# 按结构估算可见字符；图片使用 Pi 的 4800 字符估值而非 base64 长度。
def content_characters(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    characters = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "image":
            characters += 4800
        elif kind in {"text", "thinking"}:
            characters += len(str(block.get("text" if kind == "text" else "thinking", "")))
        elif kind in {"tool_use", "toolCall"}:
            characters += len(str(block.get("name", ""))) + len(json.dumps(
                block.get("input", block.get("arguments", {})),
                ensure_ascii=False, separators=(",", ":"),
            ))
        elif kind == "tool_result":
            characters += content_characters(block.get("content", ""))
    return characters


# 对每条消息独立向上取整，保留工具参数但忽略签名和媒体编码负担。
def estimate_conversation_tokens(messages: list[dict[str, Any]]) -> int:
    return sum((content_characters(message.get("content", "")) + 3) // 4 for message in messages)

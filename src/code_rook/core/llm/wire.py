from __future__ import annotations

from typing import Any


# 合并相邻 user 消息且不修改入参，避免内部注入破坏 Provider 的角色交替约束
def merge_consecutive_user_messages(
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    for message in messages:
        if (
            merged
            and message.get("role") == "user"
            and merged[-1].get("role") == "user"
        ):
            merged[-1] = {
                **merged[-1],
                "content": _concat_content(
                    merged[-1].get("content"), message.get("content")
                ),
            }
            continue
        merged.append(message)
    return merged


# 拼接两段消息内容：双字符串以空行相接，任一为块列表时把字符串归一为 text 块后原序拼接
def _concat_content(left: object, right: object) -> object:
    if isinstance(left, str) and isinstance(right, str):
        return f"{left}\n\n{right}"
    return [*_as_blocks(left), *_as_blocks(right)]


# 把 str 内容归一为 text 块列表，块列表浅拷贝返回，其余类型按文本兜底
def _as_blocks(content: object) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return list(content)
    text = content if isinstance(content, str) else str(content)
    return [{"type": "text", "text": text}]

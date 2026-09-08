# Shared context serialization adapted from Pi compaction/utils.ts (MIT).
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html import escape, unescape
from typing import Any


@dataclass
class FileOperations:
    read: set[str] = field(default_factory=set)
    modified: set[str] = field(default_factory=set)

    # 合并已生成摘要中的路径清单，跨多次压缩保留累计文件索引。
    def merge_summary(self, summary: str) -> None:
        for tag, target in (("read-files", self.read), ("modified-files", self.modified)):
            for match in re.finditer(fr"<{tag}>\n(.*?)\n</{tag}>", summary, re.DOTALL):
                target.update(unescape(path) for path in match.group(1).splitlines() if path)

    # 从工具调用提取路径，只追踪操作目标，不把它当作验证成功证据。
    def collect(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.startswith((
                "[CODEROOK_COMPACTION_V2]", "The user explored another conversation branch.",
            )):
                self.merge_summary(content)
            if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") not in {"tool_use", "toolCall"}:
                    continue
                args = block.get("input", block.get("arguments", {}))
                if not isinstance(args, dict) or not isinstance(args.get("path"), str):
                    continue
                name = block.get("name")
                if name in {"read", "read_file"}:
                    self.read.add(args["path"])
                elif name in {"write", "edit", "write_file", "edit_file"}:
                    self.modified.add(args["path"])

    # 输出排序且去重的 Pi 文件清单，修改文件不再重复列入只读列表。
    def render(self) -> str:
        sections = []
        for tag, paths in (
            ("read-files", self.read - self.modified), ("modified-files", self.modified),
        ):
            if paths:
                body = "\n".join(escape(path) for path in sorted(paths))
                sections.append(f"<{tag}>\n{body}\n</{tag}>")
        return "\n\n" + "\n\n".join(sections) if sections else ""


# 清理模型重复生成的路径标签，最终路径清单只使用代码累计的记录。
def strip_file_lists(summary: str) -> str:
    return re.sub(
        r"<(read-files|modified-files)>\n.*?\n</\1>", "", summary, flags=re.DOTALL,
    ).strip()


# 将多模态内容变成摘要用文本，不将图片 base64 或协议签名交给摘要模型。
def content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text", "")) for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


# 按角色序列化待总结历史，单个工具输出最多保留 2000 字符。
def serialize_conversation(messages: list[dict[str, Any]]) -> str:
    parts = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        text = content_text(content)
        if text:
            parts.append(f"[{'Assistant' if role == 'assistant' else 'User'}]: {text}")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "thinking":
                parts.append("[Assistant thinking]: " + str(block.get("thinking", "")))
            elif kind in {"tool_use", "toolCall"}:
                args = json.dumps(
                    block.get("input", block.get("arguments", {})), ensure_ascii=False,
                )
                parts.append(f"[Assistant tool calls]: {block.get('name')}({args})")
            elif kind == "tool_result":
                output = content_text(block.get("content"))
                if len(output) > 2000:
                    output = (
                        output[:2000] + f"\n[... {len(output) - 2000} more characters truncated]"
                    )
                if output:
                    parts.append("[Tool result]: " + output)
    return "\n\n".join(parts)

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from code_rook.core.authority import RuntimeMode
from code_rook.core.working_set import WorkingSet

_LANGUAGE_POLICY = (
    "Always use concise English for internal analysis, reasoning content, tool inputs, "
    "subagent delegation, memory, task state, and other machine-facing content unless exact "
    "source or user text must be preserved. For the final user-facing reply, follow the "
    "response language specified below. Keep code, commands, paths, identifiers, and quoted "
    "text unchanged. "
    "Never use emoji or decorative symbols in user-visible prose."
)


# 根据原始用户消息的文字系统生成回复语言提示，未知语言默认简体中文
def _response_language_hint(text: str) -> str:
    if any("\u3040" <= char <= "\u30ff" for char in text):
        return "Japanese"
    if any("\uac00" <= char <= "\ud7af" for char in text):
        return "Korean"
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return "Simplified Chinese"
    if any("\u0600" <= char <= "\u06ff" for char in text):
        return "Arabic"
    if any("\u0400" <= char <= "\u04ff" for char in text):
        return "the language used in the original user request"
    if any(char.isalpha() and char.isascii() for char in text):
        return "the language used in the original user request"
    return "Simplified Chinese"


@dataclass
class ExecutionContext:
    run_id: str
    goal: str
    max_steps: int
    prefill_messages: list[dict[str, Any]] = field(default_factory=list)
    session_notes: str = ""
    global_context: str = ""
    project_context: str = ""
    repository_context: str = ""
    repository_context_metadata: dict[str, Any] = field(default_factory=dict)
    runtime_context: str = ""
    capability_context: str = ""
    persistent_goal_context: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None
    result: str = ""
    user_request: str = ""
    runtime_mode: RuntimeMode = RuntimeMode.ACT
    working_set: WorkingSet = field(default_factory=WorkingSet)
    transient_context: str = ""
    # 工具产生的待发送图片块，随下一次模型请求注入后清空
    pending_images: list[dict[str, Any]] = field(default_factory=list)
    # skill 或 subagent 角色可覆盖默认 system prompt
    system_prompt_override: str | None = None

    # 初始化消息历史，优先使用 session 完整回放内容
    def __post_init__(self) -> None:
        if self.prefill_messages:
            self.messages = [dict(m) for m in self.prefill_messages]
        elif not self.messages:
            self.messages.append({"role": "user", "content": self.goal})
        if not self.user_request:
            self.user_request = self._latest_user_text() or self.goal

    # 从上下文中寻找最后一条真实用户文本，忽略协议中的 tool_result 用户消息
    def _latest_user_text(self) -> str:
        for message in reversed(self.messages):
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, str) and content.strip():
                return content
        return ""

    # 返回稳定系统指令层，不包含记忆、工作集与一次性诊断
    def stable_system_prompt(self, base: str) -> str:
        parts = [self.system_prompt_override if self.system_prompt_override else base]
        parts.append("\n\n## Language Policy\n" + _LANGUAGE_POLICY)
        parts.append(
            "\n\n## Response Language\n"
            "Final answer only: "
            + _response_language_hint(self.user_request)
            + ". This is based on the original user request that started the run, never on "
            "tool-result messages. Do not use the final-answer language for analysis or "
            "reasoning content; those must remain concise English."
        )
        if self.runtime_mode == RuntimeMode.PLAN:
            parts.append(
                "\n\n## Plan Mode\n"
                "Investigate the request without changing files, memory, tasks, processes, "
                "external systems, or repository state. Only read-only tools are available. "
                "Do not claim implementation or verification that did not occur. End with a "
                "concrete, ordered implementation plan that names affected components, key "
                "decisions, risks, and verification steps. The user must explicitly approve "
                "before a later Act turn may implement it."
            )
        if self.runtime_context.strip():
            parts.append("\n\n## Runtime Environment\n" + self.runtime_context.strip())
        if self.capability_context.strip():
            parts.append("\n\n## Available Extensions\n" + self.capability_context.strip())
        if self.persistent_goal_context.strip():
            parts.append("\n\n## Active Goal\n" + self.persistent_goal_context.strip())
        return "".join(parts)

    # 返回当前 run 的完整 system prompt，并追加记忆与动态上下文
    def system_prompt(self, base: str) -> str:
        parts = [self.stable_system_prompt(base)]
        if self.global_context.strip():
            parts.append("\n\n## Global Context\n" + self.global_context.strip())
        if self.project_context.strip():
            parts.append("\n\n## Project Context\n" + self.project_context.strip())
        if self.repository_context.strip():
            parts.append("\n\n" + self.repository_context.strip())
        if self.session_notes.strip():
            parts.append(
                "\n\n## Session Notes\n"
                + self.session_notes.strip()
                + "\n\nRemember important durable facts by calling note_save."
            )
        working_set = self.working_set.render_context()
        if working_set:
            parts.append("\n\n" + working_set)
        if self.transient_context.strip():
            parts.append("\n\n" + self.transient_context.strip())
        return "".join(parts)

    # 返回用于 prefix fingerprint 的稳定记忆层，不包含用户 prompt 或 transient context
    def stable_memory_text(self) -> str:
        return "\n\n".join(
            part.strip()
            for part in (
                self.global_context,
                self.project_context,
                self.repository_context,
                self.session_notes,
            )
            if part.strip()
        )

    # 保存只在下一次有效模型响应前可见的临时诊断上下文
    def set_transient_context(self, content: str) -> None:
        self.transient_context = content.strip()

    # 登记一张随下一次模型请求发送的图片块
    def add_pending_image(self, image_block: dict[str, Any]) -> None:
        self.pending_images.append(image_block)

    # 清除已经交付给模型的临时上下文，避免永久污染后续历史
    def clear_transient_context(self) -> None:
        self.transient_context = ""

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(
        self, tool_use_id: str, content: str, is_error: bool = False
    ) -> None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        if self.is_done():
            return
        self.status = "failed"
        self.reason = reason

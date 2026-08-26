from __future__ import annotations

import hashlib
import json
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TaskIntent(StrEnum):
    EXPLAIN = "explain"
    INSPECT = "inspect"
    FIX = "fix"
    REFACTOR = "refactor"
    TEST = "test"
    MULTI_FILE_CHANGE = "multi_file_change"


class TaskScope(StrEnum):
    READ_ONLY = "read_only"
    SINGLE_FILE = "single_file"
    MULTI_FILE = "multi_file"
    REPOSITORY = "repository"


class TaskRisk(StrEnum):
    READ = "read"
    MUTATE = "mutate"
    SHELL = "shell"
    EXTERNAL = "external"


class TaskStrategy(StrEnum):
    DIRECT = "direct"
    PLAN_FIRST = "plan_first"
    DELEGATE = "delegate"


class ContextPolicy(StrEnum):
    STANDARD = "standard"
    LONG_TASK = "long_task"


class TaskProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: TaskIntent
    scope: TaskScope
    risk: TaskRisk
    strategy: TaskStrategy
    context_policy: ContextPolicy
    confidence: float = Field(ge=0.0, le=1.0)
    signals: tuple[str, ...] = ()
    source: str = "rules"
    delegation_allowed: bool = False
    digest: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")

    # 返回补全稳定摘要后的不可变任务画像
    def with_digest(self) -> TaskProfile:
        payload = self.model_dump(mode="json", exclude={"digest"})
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return self.model_copy(update={"digest": digest})

    # 返回用于模型可见工具裁剪的顶层工具名集合，None 表示保留完整目录
    def model_tool_allowlist(self) -> frozenset[str] | None:
        if self.risk == TaskRisk.READ:
            return frozenset(
                {
                    "File",
                    "Git",
                    "artifact_read",
                    "git_diff",
                    "glob",
                    "grep",
                    "list_dir",
                    "read_file",
                    "read_image",
                    "memory_search",
                    "skill",
                    "task_get",
                    "task_list",
                    "tool_search",
                    "web_fetch",
                    "web_search",
                    "ask_user_question",
                }
            )
        if not self.delegation_allowed:
            return frozenset({"__all_except_delegation__"})
        return None

    # 返回只读画像对 action family 的精确动作裁剪表
    def model_action_allowlist(self) -> dict[str, frozenset[str]]:
        if self.risk != TaskRisk.READ:
            return {}
        return {
            "File": frozenset({"read", "list", "search_name", "search_content"}),
            "Git": frozenset({"status", "diff", "log", "show", "blame"}),
        }

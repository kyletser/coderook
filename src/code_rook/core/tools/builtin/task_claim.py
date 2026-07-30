from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class TaskClaimParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: int
    owner: str = "agent"
    worktree: str = ""


class TaskClaimTool(BaseTool):
    name = "task_claim"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Atomically claim an unblocked pending task for an owner and optional worktree."
    params_model = TaskClaimParams
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "owner": {"type": "string"},
            "worktree": {"type": "string"},
        },
        "required": ["task_id"],
    }

    # 绑定当前 run 的任务管理器
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 原子认领任务并返回最新任务 JSON
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = TaskClaimParams.model_validate(params)
        try:
            task = self._manager.claim(parsed.task_id, parsed.owner, parsed.worktree)
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))

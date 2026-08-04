from __future__ import annotations

import json

from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class TaskCreateTool(BaseTool):
    name = "task_create"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = (
        "Create a new task to track a unit of work. "
        "Use this to break down a complex goal into smaller, trackable steps. "
        "Returns the created task as JSON."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short title for the task.",
            },
            "description": {
                "type": "string",
                "description": "Optional longer description of what needs to be done.",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "IDs of tasks that must be completed before this one.",
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Verifiable conditions required before completion.",
            },
            "gates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Named review or verification gates for this task.",
            },
        },
        "required": ["subject"],
    }

    # 持有 TaskManager 实例，供 invoke 调用
    def __init__(self, task_manager: TaskManager) -> None:
        self._manager = task_manager

    # 创建任务并返回 JSON 字符串
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        subject = str(params["subject"])
        description = str(params.get("description") or "")
        raw_blocked: list[object] = list(params.get("blocked_by") or [])  # type: ignore[call-overload]
        blocked_by = [int(str(x)) for x in raw_blocked]
        raw_acceptance: list[object] = list(
            params.get("acceptance_criteria") or []  # type: ignore[call-overload]
        )
        raw_gates: list[object] = list(params.get("gates") or [])  # type: ignore[call-overload]
        try:
            task = self._manager.create(
                subject,
                description,
                blocked_by,
                acceptance_criteria=[str(item) for item in raw_acceptance],
                gates=[str(item) for item in raw_gates],
            )
            return ToolResult(content=json.dumps(task.to_dict(), ensure_ascii=False))
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

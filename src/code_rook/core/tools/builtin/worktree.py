from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.worktree import WorktreeError, WorktreeManager


class WorktreeCreateParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    base_ref: str = "HEAD"


class WorktreeRemoveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    discard_changes: bool = False


class WorktreeCreateTool(BaseTool):
    name = "worktree_create"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Create an isolated Git worktree under .coderook/worktrees for parallel work."
    params_model = WorktreeCreateParams
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "base_ref": {"type": "string"},
        },
        "required": ["name"],
    }

    # 绑定项目 worktree 管理器
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    # 创建隔离 worktree 并返回路径
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = WorktreeCreateParams.model_validate(params)
        try:
            path = await self._manager.create(parsed.name, parsed.base_ref)
        except WorktreeError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=f"created worktree {parsed.name}: {path}")


class WorktreeListTool(BaseTool):
    name = "worktree_list"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    description = (
        "List only CodeRook-managed Git worktrees under .coderook/worktrees."
    )
    params_model = None
    input_schema = {"type": "object", "properties": {}}

    # 绑定项目 worktree 管理器
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    # 返回受管 worktree 列表
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            payload = await self._manager.list()
        except WorktreeError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))


class WorktreeRemoveTool(BaseTool):
    name = "worktree_remove"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Remove a managed Git worktree, refusing dirty trees unless explicitly discarded."
    params_model = WorktreeRemoveParams
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "discard_changes": {"type": "boolean"},
        },
        "required": ["name"],
    }

    # 绑定项目 worktree 管理器
    def __init__(self, manager: WorktreeManager) -> None:
        self._manager = manager

    # 删除受管 worktree 并保护未提交修改
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = WorktreeRemoveParams.model_validate(params)
        try:
            await self._manager.remove(parsed.name, parsed.discard_changes)
        except WorktreeError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=f"removed worktree {parsed.name}")

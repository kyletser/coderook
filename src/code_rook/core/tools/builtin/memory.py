from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.memory import MemoryStore
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class MemorySaveParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    description: str = ""
    type: Literal["user", "feedback", "project", "reference"] = "project"
    body: str


class MemorySearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    limit: int = Field(default=5, ge=1, le=20)


class MemoryForgetParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    memory_id: str


class MemorySaveTool(BaseTool):
    name = "memory_save"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Save a durable project memory with provenance for future sessions."
    params_model = MemorySaveParams
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "description": {"type": "string"},
            "type": {
                "type": "string",
                "enum": ["user", "feedback", "project", "reference"],
            },
            "body": {"type": "string"},
        },
        "required": ["name", "body"],
    }

    # 绑定项目记忆库以及来源 session/run
    def __init__(
        self,
        store: MemoryStore,
        session_id: str,
        run_id: str,
    ) -> None:
        self._store = store
        self._session_id = session_id
        self._run_id = run_id

    # 保存一条可跨会话使用的项目记忆
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = MemorySaveParams.model_validate(params)
        try:
            record = self._store.save(
                name=parsed.name,
                description=parsed.description,
                mem_type=parsed.type,
                body=parsed.body,
                source_session_id=self._session_id,
                source_run_id=self._run_id,
            )
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=f"saved memory_id={record.id}")


class MemorySearchTool(BaseTool):
    name = "memory_search"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    description = "Search durable project memories and return their sources."
    params_model = MemorySearchParams
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
    }

    # 绑定项目记忆库
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # 检索并返回结构化记忆结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = MemorySearchParams.model_validate(params)
        records = self._store.search(parsed.query, parsed.limit)
        return ToolResult(
            content=json.dumps(
                [record.__dict__ for record in records], ensure_ascii=False, indent=2
            )
        )


class MemoryForgetTool(BaseTool):
    name = "memory_forget"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Delete a durable project memory by memory_id."
    params_model = MemoryForgetParams
    input_schema = {
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    }

    # 绑定项目记忆库
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # 删除指定记忆并报告结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        memory_id = MemoryForgetParams.model_validate(params).memory_id
        if not self._store.forget(memory_id):
            return ToolResult(
                content=f"memory not found: {memory_id}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=f"forgot memory_id={memory_id}")

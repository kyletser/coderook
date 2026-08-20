from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.repository.index import RepositoryIndex
from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ToolActionSpec,
    ToolCapability,
    ToolSpec,
)


class RepositoryParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    query: str = ""
    symbol: str = ""
    path: str = "."
    limit: int = Field(default=50, ge=1, le=500)
    budget_chars: int = Field(default=12_000, ge=1_000, le=50_000)


class RepositoryTool(BaseTool):
    name = "Repository"
    description = (
        "Inspect the Git-aware repository map, search code symbols, or find identifier "
        "references without reading every file."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["map", "symbols", "references"]},
            "query": {"type": "string"},
            "symbol": {"type": "string"},
            "path": {"type": "string", "default": "."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "budget_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
        },
        "required": ["action"],
    }
    params_model = RepositoryParams
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True

    # 初始化共享仓库索引工具
    def __init__(self, index: RepositoryIndex) -> None:
        self._index = index

    # 为三个只读 action 生成独立 schema 和 capability 契约
    def build_spec(self) -> ToolSpec:
        action_schemas: dict[str, dict[str, object]] = {
            "map": {
                "type": "object",
                "properties": {
                    "action": {"const": "map"},
                    "query": {"type": "string"},
                    "budget_chars": {"type": "integer", "minimum": 1000, "maximum": 50000},
                },
                "required": ["action"],
            },
            "symbols": {
                "type": "object",
                "properties": {
                    "action": {"const": "symbols"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["action", "query"],
            },
            "references": {
                "type": "object",
                "properties": {
                    "action": {"const": "references"},
                    "symbol": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["action", "symbol"],
            },
        }
        actions = tuple(
            ToolActionSpec(
                name=name,
                description=f"Repository {name} query",
                input_schema=schema,
                capabilities=frozenset({ToolCapability.READ}),
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            )
            for name, schema in action_schemas.items()
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            actions=actions,
            capabilities=frozenset({ToolCapability.READ}),
            approval_requirement=ApprovalRequirement.NEVER,
            parallel_policy=ParallelPolicy.SAFE,
        )

    # 分派仓库地图、符号和引用查询，并返回结构化 JSON
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = RepositoryParams.model_validate(params)
        try:
            if request.action == "map":
                selection = await asyncio.to_thread(
                    self._index.select_context,
                    request.query,
                    budget_chars=request.budget_chars,
                )
                payload: dict[str, object] = {
                    "repository_hash": selection.repository_hash,
                    "paths": list(selection.paths),
                    "selection_reasons": list(selection.reasons),
                    "budget_chars": selection.budget_chars,
                    "used_chars": selection.used_chars,
                    "map": selection.content,
                }
            elif request.action == "symbols":
                symbols = await asyncio.to_thread(
                    self._index.search_symbols,
                    request.query,
                    limit=request.limit,
                )
                payload = {
                    "query": request.query,
                    "symbols": [symbol.__dict__ for symbol in symbols],
                    "count": len(symbols),
                    "backend": "syntax-index",
                }
            elif request.action == "references":
                matches = await asyncio.to_thread(
                    self._index.find_references,
                    request.symbol,
                    path=request.path,
                    limit=request.limit,
                )
                payload = {
                    "symbol": request.symbol,
                    "matches": list(matches),
                    "count": len(matches),
                    "backend": "syntax-index+text-fallback",
                }
            else:
                return ToolResult(
                    f"unknown Repository action: {request.action}",
                    is_error=True,
                    error_type="schema_error",
                )
        except (OSError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.spec import ToolSpec

if TYPE_CHECKING:
    from code_rook.core.tools.registry import ToolRegistry


class ToolSearchMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    actions: tuple[str, ...]
    reason: str
    schema_handle: str


# 为工具 schema 生成不暴露完整 schema 的稳定内容句柄
def _schema_handle(spec: ToolSpec) -> str:
    canonical = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"tool-schema:{spec.name}:{spec.version}:{digest}"


# 对 deferred ToolSpec 进行确定性大小写无关匹配和稳定排序
def search_deferred_specs(
    specs: tuple[ToolSpec, ...],
    query: str,
    *,
    limit: int,
) -> tuple[ToolSearchMatch, ...]:
    normalized = " ".join(query.casefold().split())
    terms = tuple(part for part in normalized.split(" ") if part)
    if not normalized:
        return ()
    ranked: list[tuple[int, str, str, ToolSpec]] = []
    for spec in specs:
        name = spec.name.casefold()
        haystack = " ".join(
            (
                name,
                spec.description.casefold(),
                *(action.name.casefold() for action in spec.actions),
                *(action.description.casefold() for action in spec.actions),
            )
        )
        if normalized == name:
            score = 0
            reason = "exact_name"
        elif name.startswith(normalized):
            score = 1
            reason = "name_prefix"
        elif terms and all(term in haystack for term in terms):
            score = 2
            reason = "all_query_terms"
        elif normalized in haystack:
            score = 3
            reason = "substring"
        else:
            continue
        ranked.append((score, name, reason, spec))
    ranked.sort(key=lambda item: (item[0], item[1], item[3].name))
    return tuple(
        ToolSearchMatch(
            name=spec.name,
            description=spec.description,
            actions=tuple(action.name for action in spec.actions),
            reason=reason,
            schema_handle=_schema_handle(spec),
        )
        for _score, _name, reason, spec in ranked[:limit]
    )


class ToolSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=8)


class ToolSearchTool(BaseTool):
    name = "tool_search"
    description = (
        "Search deferred tools by capability and activate matching schemas for the next "
        "model step. Results are deterministic and bounded."
    )
    side_effect = ToolSideEffect.NONE
    can_parallel = False
    params_model = ToolSearchParams
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        "required": ["query"],
    }

    # 绑定同一运行时 registry，使搜索结果可激活下一模型步骤
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    # 搜索并激活匹配工具，返回不含完整 schema 的有界摘要
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = ToolSearchParams.model_validate(params)
        matches = self._registry.search_deferred(
            request.query,
            limit=request.limit,
        )
        return ToolResult(
            json.dumps(
                {
                    "query": request.query,
                    "activated": [match.name for match in matches],
                    "tools": [match.model_dump() for match in matches],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

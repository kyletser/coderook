from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.spec import ResolvedToolCall, ToolPresentationKind


class ToolPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=2, ge=1)
    kind: ToolPresentationKind
    title_key: str
    status: str
    subject: str = ""
    locations: tuple[str, ...] = ()
    command: str = ""
    exit_code: int | None = None
    truncated: bool = False
    total_bytes: int | None = Field(default=None, ge=0)
    artifact: dict[str, Any] | None = None
    diagnostics: str = ""
    sandbox_enforcement: str = "unavailable"
    failure_category: str = ""
    recovery_actions: tuple[str, ...] = ()


# 从可信 Manifest、调用参数和工具结果生成可持久化的纯展示数据
def build_tool_presentation(
    resolved: ResolvedToolCall,
    params: dict[str, object],
    result: ToolResult | None,
) -> ToolPresentation:
    spec = resolved.action.presentation
    subject = next(
        (
            str(params[field])
            for field in spec.subject_fields
            if field in params and params[field] is not None and params[field] != ""
        ),
        "",
    )
    locations = tuple(
        str(params[field])
        for field in spec.location_fields
        if field in params and params[field] is not None and params[field] != ""
    )
    command = str(params.get("command", "")) if spec.kind == ToolPresentationKind.TERMINAL else ""
    exit_code: int | None = None
    truncated = False
    total_bytes: int | None = None
    artifact: dict[str, Any] | None = None
    diagnostics = ""
    enforcement = "unavailable"
    failure_category = ""
    recovery_actions: tuple[str, ...] = ()
    if result is not None:
        usage = result.process_usage or {}
        raw_exit = usage.get("exit_code")
        exit_code = raw_exit if isinstance(raw_exit, int) else None
        enforcement = result.sandbox_enforcement
        diagnostics = result.error_type or ""
        failure_category = result.failure_category or ""
        if result.is_error:
            recovery_actions = (
                "review_permissions",
                "adjust_parameters",
                "retry_once",
                "stop_run",
            )
        try:
            structured = json.loads(result.content)
        except (json.JSONDecodeError, TypeError):
            structured = None
        if isinstance(structured, dict) and structured.get("kind") == "tool_output_summary":
            truncated = bool(structured.get("truncated"))
            raw_bytes = structured.get("bytes")
            total_bytes = raw_bytes if isinstance(raw_bytes, int) and raw_bytes >= 0 else None
            raw_artifact = structured.get("artifact")
            artifact = dict(raw_artifact) if isinstance(raw_artifact, dict) else None
    return ToolPresentation(
        schema_version=max(2, spec.result_schema_version),
        kind=spec.kind,
        title_key=spec.title_key,
        status="running" if result is None else "failed" if result.is_error else "succeeded",
        subject=subject,
        locations=locations,
        command=command,
        exit_code=exit_code,
        truncated=truncated,
        total_bytes=total_bytes,
        artifact=artifact,
        diagnostics=diagnostics,
        sandbox_enforcement=enforcement,
        failure_category=failure_category,
        recovery_actions=recovery_actions,
    )

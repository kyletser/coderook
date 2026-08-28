from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.spec import (
    ResolvedToolCall,
    ToolCapability,
    ToolPresentationAction,
    ToolPresentationKind,
)


class ToolPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=3, ge=1)
    kind: ToolPresentationKind
    action: ToolPresentationAction = ToolPresentationAction.GENERIC
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
    supports_live_output: bool = False


# 从可信工具名和 action-family 声明推导稳定的用户活动语义
def _presentation_action(resolved: ResolvedToolCall) -> ToolPresentationAction:
    tool = resolved.spec.name.casefold()
    action = resolved.action.name.casefold()
    if tool in {"bash", "run"}:
        if tool == "run" or action in {"tests", "verifiers"}:
            return ToolPresentationAction.RUN_TESTS
        return ToolPresentationAction.RUN_COMMAND
    if tool == "file":
        return {
            "read": ToolPresentationAction.READ_FILE,
            "list": ToolPresentationAction.BROWSE_FILES,
            "search_name": ToolPresentationAction.SEARCH_CODE,
            "search_content": ToolPresentationAction.SEARCH_CODE,
            "write": ToolPresentationAction.EDIT_CODE,
            "edit": ToolPresentationAction.EDIT_CODE,
            "patch": ToolPresentationAction.EDIT_CODE,
        }.get(action, ToolPresentationAction.GENERIC)
    if tool == "git" or tool.startswith("git_"):
        return ToolPresentationAction.GIT
    if tool in {"read_file", "read_image"}:
        return ToolPresentationAction.READ_FILE
    if tool == "list_dir":
        return ToolPresentationAction.BROWSE_FILES
    if tool in {"grep", "glob", "repository_context", "repository_search"}:
        return ToolPresentationAction.SEARCH_CODE
    if tool in {"edit_file", "write_file", "apply_patch"}:
        return ToolPresentationAction.EDIT_CODE
    if tool in {"run_tests", "run_verifiers"}:
        return ToolPresentationAction.RUN_TESTS
    if tool in {"web_fetch", "web_search"} or ToolCapability.NETWORK in resolved.capabilities:
        return ToolPresentationAction.WEB
    if tool in {"agent", "spawn_agent"}:
        return ToolPresentationAction.WORKER
    return ToolPresentationAction.GENERIC


# 在旧 Manifest 未声明展示细节时按 action 语义补全稳定展示类型
def _presentation_kind(
    declared: ToolPresentationKind,
    action: ToolPresentationAction,
) -> ToolPresentationKind:
    if declared != ToolPresentationKind.GENERIC:
        return declared
    return {
        ToolPresentationAction.RUN_COMMAND: ToolPresentationKind.TERMINAL,
        ToolPresentationAction.RUN_TESTS: ToolPresentationKind.TERMINAL,
        ToolPresentationAction.READ_FILE: ToolPresentationKind.READ,
        ToolPresentationAction.BROWSE_FILES: ToolPresentationKind.READ,
        ToolPresentationAction.SEARCH_CODE: ToolPresentationKind.SEARCH,
        ToolPresentationAction.EDIT_CODE: ToolPresentationKind.DIFF,
        ToolPresentationAction.WEB: ToolPresentationKind.WEB,
    }.get(action, ToolPresentationKind.GENERIC)


# 从可信 Manifest、调用参数和工具结果生成可持久化的纯展示数据
def build_tool_presentation(
    resolved: ResolvedToolCall,
    params: dict[str, object],
    result: ToolResult | None,
) -> ToolPresentation:
    spec = resolved.action.presentation
    action = _presentation_action(resolved)
    kind = _presentation_kind(spec.kind, action)
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
    if not locations and action in {
        ToolPresentationAction.READ_FILE,
        ToolPresentationAction.BROWSE_FILES,
        ToolPresentationAction.SEARCH_CODE,
        ToolPresentationAction.EDIT_CODE,
        ToolPresentationAction.GIT,
    }:
        raw_path = params.get("path")
        locations = (str(raw_path),) if raw_path not in {None, ""} else ()
    if not subject:
        for field in ("query", "pattern", "path", "revision", "job_id"):
            value = params.get(field)
            if value not in {None, ""}:
                subject = str(value)
                break
    command = (
        str(params.get("command", ""))
        if action in {ToolPresentationAction.RUN_COMMAND, ToolPresentationAction.RUN_TESTS}
        else ""
    )
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
        schema_version=max(3, spec.result_schema_version),
        kind=kind,
        action=action,
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
        supports_live_output=(
            spec.supports_live_output
            or action in {ToolPresentationAction.RUN_COMMAND, ToolPresentationAction.RUN_TESTS}
        ),
    )

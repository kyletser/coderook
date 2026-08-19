from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from code_rook.core.artifacts import ArtifactError, ArtifactStore
from code_rook.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.errors import RateLimitedError
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import OutputPolicy, ToolCaller, ToolCatalogError

if TYPE_CHECKING:
    from code_rook.core.hooks import HookManager
    from code_rook.core.permissions.manager import PermissionManager

_DEFAULT_TIMEOUT: float = 120.0
_MAX_RETRIES: int = 2
_RETRY_BASE_S: float = 2.0  # backoff base; tests can monkeypatch to 0


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 按 UTF-8 字节预算生成包含头尾的可读预览
def _preview_bytes(content: str, limit: int) -> str:
    raw = content.encode("utf-8")
    if len(raw) <= limit:
        return content
    marker = b"\n...[output omitted]...\n"
    if limit <= len(marker):
        return raw[:limit].decode("utf-8", errors="ignore")
    available = limit - len(marker)
    head_size = available // 2
    tail_size = available - head_size
    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[-tail_size:].decode("utf-8", errors="ignore")
    return f"{head}{marker.decode()}{tail}"


# 将截断结果编码为带字节数和头尾预览的有界 typed summary
def _summary_result(
    result: ToolResult,
    *,
    size: int,
    preview_limit: int,
    hard_limit: int,
    artifact: dict[str, object] | None = None,
    artifact_error: str | None = None,
) -> ToolResult:
    payload: dict[str, object] = {
        "kind": "tool_output_summary",
        "bytes": size,
        "preview": _preview_bytes(result.content, preview_limit),
        "truncated": True,
    }
    if artifact is not None:
        payload["artifact"] = artifact
    if artifact_error is not None:
        payload["artifact_error"] = artifact_error
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(content.encode("utf-8")) > hard_limit:
        content = _preview_bytes(result.content, hard_limit)
    return ToolResult(
        content,
        is_error=result.is_error,
        error_type=result.error_type,
        images=result.images,
        process_usage=result.process_usage,
    )


# 对 soft 到 hard 输出返回摘要，只有超过 hard limit 才写入 artifact
async def _apply_output_policy(
    result: ToolResult,
    policy: OutputPolicy,
    artifact_store: ArtifactStore | None,
) -> ToolResult:
    size = len(result.content.encode("utf-8"))
    if size <= policy.soft_limit:
        return result
    preview_limit = min(policy.soft_limit, max(1, policy.hard_limit // 2))
    if size <= policy.hard_limit:
        return _summary_result(
            result,
            size=size,
            preview_limit=preview_limit,
            hard_limit=policy.hard_limit,
        )
    if artifact_store is not None and policy.spill_to_artifact:
        try:
            reference = await artifact_store.put(result.content)
            return _summary_result(
                result,
                size=size,
                preview_limit=preview_limit,
                hard_limit=policy.hard_limit,
                artifact=reference.model_dump(),
            )
        except (ArtifactError, OSError) as exc:
            return _summary_result(
                result,
                size=size,
                preview_limit=preview_limit,
                hard_limit=policy.hard_limit,
                artifact_error=str(exc),
            )
    return _summary_result(
        result,
        size=size,
        preview_limit=preview_limit,
        hard_limit=policy.hard_limit,
    )


# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
    *,
    attempt: int = 1,
    process_usage: dict[str, object] | None = None,
) -> ToolResult:
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
            process_usage=process_usage or {},
            attempt=attempt,
            ts=_now(),
        )
    )
    return ToolResult(
        content=error_message,
        is_error=True,
        error_type=error_class,
        process_usage=process_usage,
    )


# 校验参数、检查权限、限时调用工具、发布进度事件，失败时指数退避重试，返回 ToolResult（不抛异常）
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
    hooks: HookManager | None = None,
    caller: ToolCaller | str = ToolCaller.MODEL,
    artifact_store: ArtifactStore | None = None,
) -> ToolResult:
    t0 = time.monotonic()

    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus, run_id, tool_call,
            "runtime_error", f"unknown tool: {tool_call.name}", elapsed(),
        )

    try:
        resolved_call = registry.resolve_call(
            tool_call.name,
            dict(tool_call.input),
            caller=caller,
        )
    except ToolCatalogError as exc:
        return await _fail(
            bus, run_id, tool_call,
            "schema_error", str(exc), elapsed(),
        )
    invoke_params = dict(tool_call.input)

    try:
        execution_tool, invoke_params = tool.execution_target(invoke_params)
    except Exception as exc:
        return await _fail(
            bus,
            run_id,
            tool_call,
            "schema_error",
            f"could not resolve tool action: {exc}",
            elapsed(),
        )

    if execution_tool.params_model is not None:
        try:
            execution_tool.params_model.model_validate(invoke_params)
        except ValidationError as exc:
            return await _fail(
                bus, run_id, tool_call,
                "schema_error", str(exc), elapsed(),
            )

    try:
        approval_context = execution_tool.approval_context(invoke_params)
    except Exception as exc:
        return await _fail(
            bus,
            run_id,
            tool_call,
            "runtime_error",
            f"could not prepare approval context: {exc}",
            elapsed(),
        )

    if hooks is not None:
        hook_decision = await hooks.emit(
            "tool_call_before",
            {
                "run_id": run_id,
                "session_id": session_id,
                "tool_use_id": tool_call.id,
                "tool_name": tool_call.name,
                "params": dict(tool_call.input),
            },
        )
        if hook_decision.blocked:
            return await _fail(
                bus,
                run_id,
                tool_call,
                "hook_denied",
                hook_decision.reason or "Tool call blocked by PreToolUse hook.",
                elapsed(),
            )

    if permission_manager is not None:
        async def _emit_permission(raw: dict[str, Any]) -> None:
            if approval_context is not None:
                raw = dict(raw)
                raw_params = dict(raw.get("params", {}))
                raw_params["_approval_context"] = approval_context
                raw["params"] = raw_params
            if hooks is not None:
                await hooks.emit("approval_requested", dict(raw, run_id=run_id))
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        allowed, decision = await permission_manager.check_and_wait(
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id,
            event_emitter=_emit_permission,
            action=resolved_call.action.authority_action(),
            approval_requirement=resolved_call.effective_approval_requirement,
        )
        response_metadata = permission_manager.take_response_metadata(tool_call.id)
        if allowed:
            selected_hunks = response_metadata.get("selected_hunks")
            if selected_hunks is not None and approval_context is not None:
                patch_plan = approval_context.get("patch_plan")
                if isinstance(patch_plan, dict):
                    invoke_params["selected_hunks"] = selected_hunks
                    invoke_params["expected_plan_id"] = response_metadata.get(
                        "patch_plan_id"
                    ) or patch_plan.get("id")
            if decision not in ("auto_allow",):
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            fail_fast = decision == "headless_fail_fast"
            return await _fail(
                bus, run_id, tool_call,
                "permission_required" if fail_fast else "permission_denied",
                (
                    "Permission approval is required, but this headless run uses fail-fast "
                    "mode. Re-run with --permission-mode allow-list and an explicit "
                    "--allow-tool entry."
                    if fail_fast
                    else "Permission denied by policy. You may not execute this command. "
                    "Try an alternative approach or explain what permission is needed."
                ),
                elapsed(),
            )

    for attempt in range(1, _MAX_RETRIES + 2):
        error_class: str | None = None
        error_message: str | None = None
        attempt_process_usage: dict[str, object] | None = None

        try:
            effective_timeout = (
                timeout
                if execution_tool.timeout_s is None
                else execution_tool.timeout_s
            )
            if effective_timeout > 0:
                result = await asyncio.wait_for(
                    execution_tool.invoke(dict(invoke_params)),
                    timeout=effective_timeout,
                )
            else:
                result = await execution_tool.invoke(dict(invoke_params))
            result = await _apply_output_policy(
                result,
                execution_tool.build_spec().output_policy,
                artifact_store,
            )
            attempt_process_usage = result.process_usage
            ms = elapsed()

            if result.is_error:
                error_class = result.error_type or "runtime_error"
                error_message = result.content
            else:
                await bus.publish(
                    ToolCallFinishedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        tool_name=tool_call.name,
                        elapsed_ms=ms,
                        output=result.content,
                        process_usage=result.process_usage or {},
                        ts=_now(),
                    )
                )
                if hooks is not None:
                    await hooks.emit(
                        "tool_call_after",
                        {
                            "run_id": run_id,
                            "session_id": session_id,
                            "tool_use_id": tool_call.id,
                            "tool_name": tool_call.name,
                            "params": dict(invoke_params),
                            "output": result.content,
                            "is_error": False,
                        },
                    )
                return result

        except RateLimitedError as exc:
            error_class = "rate_limited"
            error_message = str(exc)
        except PermissionError as exc:
            error_class = "permission_denied"
            error_message = str(exc)
        except TimeoutError:
            return await _fail(
                bus, run_id, tool_call,
                "timeout", f"tool timed out after {timeout}s", elapsed(),
                attempt=attempt,
            )
        except Exception as exc:
            error_class = "runtime_error"
            error_message = str(exc)

        assert error_class is not None and error_message is not None
        ms = elapsed()

        if execution_tool.can_retry(error_class) and attempt <= _MAX_RETRIES:
            await bus.publish(
                ToolCallFailedEvent(
                    run_id=run_id,
                    tool_use_id=tool_call.id,
                    tool_name=tool_call.name,
                    error_class=error_class,
                    error_message=error_message,
                    elapsed_ms=ms,
                    process_usage=attempt_process_usage or {},
                    attempt=attempt,
                    terminal=False,
                    ts=_now(),
                )
            )
            await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            continue

        return await _fail(
            bus, run_id, tool_call,
            error_class, error_message, ms,
            attempt=attempt,
            process_usage=attempt_process_usage,
        )

    # unreachable, but keeps mypy happy
    return ToolResult(content="internal error", is_error=True, error_type="runtime_error")

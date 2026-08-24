from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from pydantic import BaseModel, JsonValue, ValidationError

from code_rook.core.bus.events import LlmUsageEvent
from code_rook.core.config import get_config
from code_rook.core.fleet.models import LocalWorkerRequest
from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry
from code_rook.core.permissions import PermissionManager
from code_rook.core.runner import AgentRunner
from code_rook.core.subagent.tool import parse_worker_result
from code_rook.core.workflow import WorkerExecutionResult


class _UsageCollector:
    # 初始化当前 worker process 的 token 累加器
    def __init__(self) -> None:
        self.total = 0

    # 收集每次模型调用发布的 input/output token
    async def handle(self, event: BaseModel) -> None:
        if isinstance(event, LlmUsageEvent):
            self.total += event.input_tokens + event.output_tokens


# 按固定 route/model 解析不含密钥的实际路由与进程内凭据
def _resolve_route(request: LocalWorkerRequest) -> ResolvedRoute:
    registry = RouteRegistry(get_config().llm)
    resolved = registry.resolve(request.step.route or None)
    if not request.step.model or request.step.model == resolved.route.model:
        return resolved
    route = resolved.route.model_copy(update={"model": request.step.model})
    return ResolvedRoute(
        route=route,
        receipt=route.receipt(resolved.receipt.credential_source),
        credential=resolved.credential,
    )


# 从 reviewer 最终摘要提取显式 APPROVED 布尔值
def _parse_approval(text: str) -> bool | None:
    match = re.search(r"\bAPPROVED\s*:\s*(true|false)\b", text, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).casefold() == "true"


# 在独立本地进程内执行 AgentRunner 并转换为有界 WorkerExecutionResult
async def _execute(request: LocalWorkerRequest) -> WorkerExecutionResult:
    config = get_config()
    route = _resolve_route(request)
    permission_manager = PermissionManager(timeout_s=0)
    permission_manager.set_authority_snapshot("", request.step.authority_ceiling)
    permission_manager.set_session_mode("", "fail_fast")
    usage = _UsageCollector()
    runner = AgentRunner(
        config,
        workspace_root=Path(request.workspace),
        runs_dir=Path("~/.coderook/fleet-runs").expanduser(),
        permission_manager=permission_manager,
        extra_handlers=[usage.handle],
    )
    goal = request.step.prompt
    if request.step.acceptance:
        goal += "\n\nAcceptance criteria:\n- " + "\n- ".join(request.step.acceptance)
    goal += (
        "\n\nReturn a concise handoff with sections SUMMARY, CHANGES, EVIDENCE, "
        "RISKS, and BLOCKERS."
    )
    if request.step.profile == "reviewer":
        goal += " Include exactly APPROVED: true or APPROVED: false in SUMMARY."
    outcome = await runner.run_and_capture(
        goal,
        run_id=request.worker_id.replace(":", "-"),
        runtime_mode=request.step.authority_ceiling.mode,
        resolved_route=route,
        resolved_route_is_explicit=True,
    )
    parsed = parse_worker_result(outcome.result)
    status = "completed" if outcome.status == "success" else "failed"
    summary = str(parsed["summary"] or outcome.reason or status)[:4_000]
    receipt: dict[str, JsonValue] = {
        "route": route.receipt.route_id,
        "wire_format": route.receipt.wire_format,
        "model": route.receipt.model,
        "reasoning": request.step.reasoning,
        "attempt": request.attempt,
    }
    return WorkerExecutionResult(
        status=status,
        summary=summary,
        evidence=[str(item) for item in parsed["evidence"]],
        token_usage=usage.total,
        approved=_parse_approval(outcome.result),
        receipt=receipt,
    )


# 读取单个 stdin JSON 请求并只向 stdout 写一个结构化结果
def main() -> None:
    try:
        raw = sys.stdin.buffer.readline(1_048_577)
        if not raw or len(raw) > 1_048_576:
            raise ValueError("invalid local worker request size")
        request = LocalWorkerRequest.model_validate_json(raw)
        result = asyncio.run(_execute(request))
    except (ValidationError, ValueError) as exc:
        result = WorkerExecutionResult(
            status="failed",
            summary=f"invalid local worker request: {type(exc).__name__}",
        )
    except BaseException as exc:
        result = WorkerExecutionResult(
            status="failed",
            summary=f"local worker failed: {type(exc).__name__}",
        )
    sys.stdout.write(result.model_dump_json())
    sys.stdout.flush()


if __name__ == "__main__":
    main()

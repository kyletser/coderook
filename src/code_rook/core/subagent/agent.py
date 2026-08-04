from __future__ import annotations

import asyncio
import copy
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from code_rook.core.subagent.models import WorkerRecord, WorkerStatus
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.store import WorkerStoreError
from code_rook.core.subagent.tool import SpawnAgentTool, worker_result_payload
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ToolActionSpec,
    ToolCapability,
    ToolSpec,
)


class _StatusParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    worker_id: str = ""
    root_goal_id: str = ""
    limit: int = Field(default=20, ge=1, le=100)


class _PeekParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    worker_id: str = Field(min_length=1)
    after_cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class _WaitParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    worker_id: str = Field(min_length=1)
    timeout_s: float = Field(default=30.0, ge=0, le=60)


class _CancelParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    worker_id: str = Field(min_length=1)


class _FollowupParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    worker_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=8_000)


# 将 WorkerRecord 转成不含 prompt、凭据和完整 transcript 的状态对象
def _status_object(worker: WorkerRecord) -> dict[str, object]:
    return {
        "worker_id": worker.id,
        "parent_turn_id": worker.parent_turn_id,
        "root_goal_id": worker.root_goal_id,
        "role": worker.role,
        "profile": worker.profile,
        "route": worker.route,
        "model": worker.model,
        "status": worker.status.value,
        "status_reason": worker.status_reason,
        "depth": worker.depth,
        "attempt": worker.attempt,
        "max_attempts": worker.max_attempts,
        "heartbeat_at": worker.heartbeat_at,
        "lease_timeout_s": worker.lease_timeout_s,
        "token_budget": worker.token_budget,
        "token_usage": worker.token_usage,
        "event_cursor": worker.event_cursor,
        "summary": worker.summary[:1_000],
        "blockers": worker.blockers[:10],
    }


class AgentTool(BaseTool):
    name = "agent"
    description = (
        "Manage durable subagents with start, status, peek, wait, cancel, and followup actions."
    )
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 绑定共享持久 registry 和兼容的 SpawnAgent 启动 backend
    def __init__(
        self,
        registry: BackgroundTaskRegistry,
        spawn_backend: SpawnAgentTool,
    ) -> None:
        self._registry = registry
        self._spawn = spawn_backend

    # 返回六个 agent action 的独立能力、审批和输入契约
    def build_spec(self) -> ToolSpec:
        start_schema = copy.deepcopy(self._spawn.input_schema)
        properties = start_schema.get("properties")
        if isinstance(properties, dict):
            properties.pop("run_in_background", None)
        start_schema.pop("required", None)
        start_schema["oneOf"] = [
            {"required": ["description", "prompt"]},
            {"required": ["worker_id"]},
        ]
        read = frozenset({ToolCapability.READ})
        external = frozenset({ToolCapability.EXTERNAL})
        actions = (
            ToolActionSpec(
                name="start",
                description="Start a durable background worker after validating its write claim.",
                input_schema=start_schema,
                capabilities=external,
                approval_requirement=ApprovalRequirement.POLICY,
                parallel_policy=ParallelPolicy.SERIAL,
            ),
            ToolActionSpec(
                name="status",
                description="Query one worker or list durable workers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "worker_id": {"type": "string"},
                        "root_goal_id": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                },
                capabilities=read,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="peek",
                description="Read bounded progress events after a cursor.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "worker_id": {"type": "string"},
                        "after_cursor": {"type": "integer", "minimum": 0},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["worker_id"],
                },
                capabilities=read,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="wait",
                description="Wait up to 60 seconds for a worker and return its current result.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "worker_id": {"type": "string"},
                        "timeout_s": {"type": "number", "minimum": 0, "maximum": 60},
                    },
                    "required": ["worker_id"],
                },
                capabilities=read,
                approval_requirement=ApprovalRequirement.NEVER,
                parallel_policy=ParallelPolicy.SAFE,
            ),
            ToolActionSpec(
                name="cancel",
                description="Cancel a running worker and record a durable terminal state.",
                input_schema={
                    "type": "object",
                    "properties": {"worker_id": {"type": "string"}},
                    "required": ["worker_id"],
                },
                capabilities=external,
                approval_requirement=ApprovalRequirement.POLICY,
                parallel_policy=ParallelPolicy.SERIAL,
            ),
            ToolActionSpec(
                name="followup",
                description="Send a bounded followup instruction to a running worker.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "worker_id": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["worker_id", "message"],
                },
                capabilities=external,
                approval_requirement=ApprovalRequirement.POLICY,
                parallel_policy=ParallelPolicy.SERIAL,
            ),
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            actions=actions,
            capabilities=read | external,
            approval_requirement=ApprovalRequirement.POLICY,
            parallel_policy=ParallelPolicy.SERIAL,
        )

    # 查询单个或按 root goal 过滤后的持久 Worker 状态
    def _status(self, params: dict[str, object]) -> ToolResult:
        request = _StatusParams.model_validate(params)
        if request.worker_id:
            worker = self._registry.record(request.worker_id)
            if worker is None:
                return ToolResult(
                    f"Unknown worker_id: {request.worker_id}",
                    is_error=True,
                    error_type="runtime_error",
                )
            payload: object = _status_object(worker)
        else:
            workers = self._registry.list_records()
            if request.root_goal_id:
                workers = [
                    item
                    for item in workers
                    if item.root_goal_id == request.root_goal_id
                ]
            payload = [_status_object(item) for item in workers[-request.limit :]]
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    # 读取指定 Worker 游标后的有界摘要事件
    def _peek(self, params: dict[str, object]) -> ToolResult:
        request = _PeekParams.model_validate(params)
        events = self._registry.events(
            request.worker_id,
            after_cursor=request.after_cursor,
            limit=request.limit,
        )
        payload = [event.model_dump(mode="json") for event in events]
        return ToolResult(json.dumps(payload, ensure_ascii=False, indent=2))

    # 等待当前进程任务完成或超时，再返回持久结构化结果
    async def _wait(self, params: dict[str, object]) -> ToolResult:
        request = _WaitParams.model_validate(params)
        worker = self._registry.record(request.worker_id)
        if worker is None:
            return ToolResult(
                f"Unknown worker_id: {request.worker_id}",
                is_error=True,
                error_type="runtime_error",
            )
        live = self._registry.get(request.worker_id)
        if live is not None and not live[0].done() and request.timeout_s > 0:
            try:
                await asyncio.wait_for(asyncio.shield(live[0]), request.timeout_s)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                if not live[0].cancelled():
                    raise
        worker = self._registry.record(request.worker_id) or worker
        if worker.status in {
            WorkerStatus.QUEUED,
            WorkerStatus.RUNNING,
            WorkerStatus.WAITING,
            WorkerStatus.INTERRUPTED,
        }:
            return self._status({"worker_id": worker.id})
        return ToolResult(
            worker_result_payload(worker),
            is_error=worker.status
            in {
                WorkerStatus.FAILED,
                WorkerStatus.CANCELLED,
                WorkerStatus.BUDGET_LIMITED,
            },
            error_type=(
                "runtime_error"
                if worker.status
                in {
                    WorkerStatus.FAILED,
                    WorkerStatus.CANCELLED,
                    WorkerStatus.BUDGET_LIMITED,
                }
                else None
            ),
        )

    # 校验 action 并分派到 Durable Worker 控制面
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        action = params.get("action")
        payload = dict(params)
        payload.pop("action", None)
        try:
            if action == "start":
                worker_id = payload.get("worker_id")
                if isinstance(worker_id, str) and worker_id:
                    existing = self._registry.record(worker_id)
                    if existing is None:
                        return ToolResult(
                            f"Unknown worker_id: {worker_id}",
                            is_error=True,
                            error_type="runtime_error",
                        )
                    payload.setdefault("description", existing.description)
                    payload.setdefault("prompt", existing.prompt)
                    payload.setdefault("subagent_type", existing.profile)
                    payload.setdefault("worktree", existing.worktree)
                    payload.setdefault("read_only", existing.write_claim.read_only)
                    payload.setdefault("exact_files", existing.write_claim.exact_files)
                    payload.setdefault("write_roots", existing.write_claim.write_roots)
                    payload.setdefault(
                        "coordination_contract",
                        existing.write_claim.coordination_contract,
                    )
                    payload.setdefault("merge_owner", existing.merge_owner)
                    payload.setdefault("merge_reviewer", existing.merge_reviewer)
                    payload.setdefault("dependencies", existing.dependencies)
                    payload.setdefault("acceptance", existing.acceptance)
                    payload.setdefault("token_budget", existing.token_budget)
                    payload.setdefault("wall_time_s", existing.wall_time_s)
                    payload.setdefault("max_attempts", existing.max_attempts)
                    payload.setdefault("retry_backoff_s", existing.retry_backoff_s)
                payload["run_in_background"] = True
                return await self._spawn.invoke(payload)
            if action == "status":
                return self._status(payload)
            if action == "peek":
                return self._peek(payload)
            if action == "wait":
                return await self._wait(payload)
            if action == "cancel":
                cancel_request = _CancelParams.model_validate(payload)
                worker = await self._registry.cancel(cancel_request.worker_id)
                return ToolResult(worker_result_payload(worker))
            if action == "followup":
                followup_request = _FollowupParams.model_validate(payload)
                worker = self._spawn.followup(
                    followup_request.worker_id,
                    followup_request.message,
                )
                return ToolResult(
                    json.dumps(_status_object(worker), ensure_ascii=False, indent=2)
                )
        except (ValidationError, ValueError, WorkerStoreError) as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
        return ToolResult(
            f"unknown agent action: {action}",
            is_error=True,
            error_type="schema_error",
        )

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class BackgroundStartParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str
    timeout: int = Field(default=120, ge=1, le=3600)


class BackgroundJobParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str


class BackgroundStartTool(BaseTool):
    name = "background_start"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Start a slow shell command in the daemon and return a durable job_id."
    params_model = BackgroundStartParams
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
        },
        "required": ["command"],
    }

    # 绑定 daemon 级任务表及当前来源信息
    def __init__(
        self,
        registry: BackgroundJobRegistry,
        session_id: str,
        run_id: str,
    ) -> None:
        self._registry = registry
        self._session_id = session_id
        self._run_id = run_id

    # 启动后台 shell 命令并返回 job_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = BackgroundStartParams.model_validate(params)
        if not parsed.command.strip():
            return ToolResult(content="empty command", is_error=True, error_type="runtime_error")
        job = self._registry.start(
            parsed.command,
            parsed.timeout,
            self._session_id,
            self._run_id,
        )
        return ToolResult(
            content=(
                f"Background job started: job_id={job.id}. "
                "Use background_result to retrieve its output."
            )
        )


class BackgroundResultTool(BaseTool):
    name = "background_result"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    description = "Get status and output for a daemon background job."
    params_model = BackgroundJobParams
    input_schema = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    }

    # 绑定 daemon 级任务表
    def __init__(self, registry: BackgroundJobRegistry) -> None:
        self._registry = registry

    # 查询任务状态和完整输出
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        job_id = BackgroundJobParams.model_validate(params).job_id
        job = self._registry.get(job_id)
        if job is None:
            return ToolResult(
                content=f"unknown background job: {job_id}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(
            content=json.dumps(job.__dict__, ensure_ascii=False, indent=2),
            is_error=job.status == "failed",
            error_type="runtime_error" if job.status == "failed" else None,
        )


class BackgroundListTool(BaseTool):
    name = "background_list"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    description = (
        "List CodeRook-internal shell jobs started by background_start in the current session. "
        "Its scope is only jobs recorded by CodeRook."
    )
    params_model = None
    input_schema = {"type": "object", "properties": {}}

    # 绑定 daemon 级任务表和当前 session
    def __init__(self, registry: BackgroundJobRegistry, session_id: str) -> None:
        self._registry = registry
        self._session_id = session_id

    # 列出当前 session 的后台任务摘要
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        jobs = self._registry.list(self._session_id)
        payload = [
            {"job_id": job.id, "command": job.command, "status": job.status}
            for job in jobs
        ]
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, indent=2))


class BackgroundCancelTool(BaseTool):
    name = "background_cancel"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = "Cancel a running daemon background job."
    params_model = BackgroundJobParams
    input_schema = {
        "type": "object",
        "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"],
    }

    # 绑定 daemon 级任务表
    def __init__(self, registry: BackgroundJobRegistry) -> None:
        self._registry = registry

    # 取消仍在运行的任务
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        job_id = BackgroundJobParams.model_validate(params).job_id
        if not await self._registry.cancel(job_id):
            return ToolResult(
                content=f"background job is not running: {job_id}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=f"cancelled background job: {job_id}")

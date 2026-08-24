from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from code_rook.core.goal import GoalService, GoalStoreError
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class GoalUpdateParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["completed", "blocked"]
    evidence: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    # 要求完成 Goal 时提供至少一条可追溯证据，阻塞状态则必须解释原因
    def _require_supporting_detail(self) -> GoalUpdateParams:
        clean_evidence = [item.strip() for item in self.evidence if item.strip()]
        if self.status == "completed" and not clean_evidence:
            raise ValueError("completed status requires at least one evidence reference")
        if self.status == "blocked" and not self.summary.strip():
            raise ValueError("blocked status requires a summary explaining the blocker")
        self.evidence = clean_evidence
        return self


class GoalUpdateTool(BaseTool):
    name = "update_goal"
    description = (
        "Update the active durable goal only when it is genuinely completed or blocked. "
        "Completion requires a daemon-verified evidence reference emitted after a passing "
        "verification event; use latest-verification for the newest passing gate. A normal "
        "model response, file path, or claimed test result is not completion evidence."
    )
    params_model = GoalUpdateParams
    side_effect = ToolSideEffect.LOCAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["completed", "blocked"],
                "description": "The verified terminal state of the durable goal.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                "maxItems": 20,
                "description": (
                    "Daemon-verified references, or latest-verification for the newest "
                    "passing verification gate."
                ),
            },
            "summary": {
                "type": "string",
                "maxLength": 4000,
                "description": "Concise completion evidence summary or blocker explanation.",
            },
        },
        "required": ["status"],
        "additionalProperties": False,
    }

    # 绑定当前持久 Goal，使模型不能修改其他 session 的目标
    def __init__(self, service: GoalService, goal_id: str) -> None:
        self._service = service
        self._goal_id = goal_id

    # 校验显式证据后完成 Goal，或把无法继续的 Goal 标记为 blocked
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = GoalUpdateParams.model_validate(params)
        try:
            if parsed.status == "completed":
                goal = self._service.complete(
                    self._goal_id,
                    evidence=[("verified-run", item) for item in parsed.evidence],
                    summary=parsed.summary,
                )
            else:
                goal = self._service.set_status(
                    self._goal_id,
                    "blocked",
                    reason=parsed.summary,
                    actor="agent",
                )
        except (GoalStoreError, ValueError) as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=f"goal {goal.id} marked {goal.status}")

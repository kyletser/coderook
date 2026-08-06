from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.interaction import InteractionManager
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class AskUserQuestionParams(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=1000)
    header: str = Field(default="Question", min_length=1, max_length=40)
    options: list[str] = Field(default_factory=list, max_length=6)
    multi_select: bool = False


class AskUserQuestionTool(BaseTool):
    name = "ask_user_question"
    description = (
        "Ask the user one focused clarification and wait for their answer. "
        "Use this when repository inspection cannot safely resolve a material choice. "
        "Provide concise options when useful; leave options empty for a free-form answer."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": 1000},
            "header": {"type": "string", "minLength": 1, "maxLength": 40},
            "options": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 200},
                "maxItems": 6,
            },
            "multi_select": {"type": "boolean"},
        },
        "required": ["question"],
    }
    params_model = AskUserQuestionParams
    side_effect = ToolSideEffect.NONE
    # 阻塞等待人类回答，豁免默认工具超时
    timeout_s = 0.0

    # 初始化结构化提问工具的会话与运行标识
    def __init__(
        self,
        manager: InteractionManager,
        session_id: str,
        run_id: str,
    ) -> None:
        self._manager = manager
        self._session_id = session_id
        self._run_id = run_id

    # 发布问题并等待用户答案，将答案作为工具结果交回模型
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = AskUserQuestionParams.model_validate(params)
        options = [option.strip() for option in parsed.options if option.strip()]
        answer = await self._manager.ask(
            run_id=self._run_id,
            session_id=self._session_id,
            question=parsed.question.strip(),
            header=parsed.header.strip(),
            options=options,
            multi_select=parsed.multi_select,
        )
        return ToolResult(content=f"User answer: {answer}")

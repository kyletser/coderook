from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from code_rook.core.skills.loader import SkillError, SkillLoader
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect


class SkillParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str
    arguments: str = ""


class SkillTool(BaseTool):
    name = "skill"
    side_effect = ToolSideEffect.NONE
    description = (
        "Load the full instructions for an available CodeRook skill whose description "
        "matches the user's request. Do not use this merely to list skills."
    )
    params_model = SkillParams

    # 绑定加载器与冻结的 workspace trust，仅把可信 skill 元数据写入模型 schema
    def __init__(
        self,
        loader: SkillLoader,
        *,
        workspace_trusted: bool = False,
    ) -> None:
        self._loader = loader
        self._workspace_trusted = workspace_trusted
        skills = loader.list_for_execution(workspace_trusted=workspace_trusted)
        self._expected_digests = {skill.name: skill.digest for skill in skills}
        names = [skill.name for skill in sorted(skills, key=lambda item: item.name)]
        descriptions = "; ".join(
            f"{skill.name}: {' '.join(skill.description.split())}"
            for skill in sorted(skills, key=lambda item: item.name)
        )
        if descriptions:
            self.description += f" Available skills: {descriptions}"
        name_schema: dict[str, Any] = {
            "type": "string",
            "description": "Exact skill name selected from the available skills.",
        }
        if names:
            name_schema["enum"] = names
        self.input_schema = {
            "type": "object",
            "properties": {
                "name": name_schema,
                "arguments": {
                    "type": "string",
                    "description": "The user's task or arguments to pass to the skill.",
                },
            },
            "required": ["name"],
        }

    # 加载选中 skill 的完整正文并替换参数占位符
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = SkillParams.model_validate(params)
        expected_digest = self._expected_digests.get(parsed.name)
        if expected_digest is None:
            return ToolResult(
                content=(
                    f"Unknown skill or unavailable in this trust context: {parsed.name}"
                ),
                is_error=True,
                error_type="runtime_error",
            )
        try:
            skill = self._loader.resolve(
                parsed.name,
                require_trusted=True,
                workspace_trusted=self._workspace_trusted,
                expected_digest=expected_digest,
            )
        except SkillError as exc:
            return ToolResult(
                content=str(exc),
                is_error=True,
                error_type="integrity_error",
            )
        if skill is None:
            return ToolResult(
                content=f"Unknown skill: {parsed.name}",
                is_error=True,
                error_type="runtime_error",
            )
        instructions = self._loader.render_prompt(
            skill,
            parsed.arguments,
            require_trusted=True,
            workspace_trusted=self._workspace_trusted,
            expected_digest=expected_digest,
        )
        allowed = ", ".join(skill.allowed_tools) if skill.allowed_tools else "all available"
        return ToolResult(
            content=(
                f"Loaded skill '{skill.name}'. Follow these instructions for the current "
                f"task. Declared tools: {allowed}.\n\n{instructions}"
            )
        )

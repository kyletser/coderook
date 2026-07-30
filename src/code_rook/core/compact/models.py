from __future__ import annotations

from pydantic import BaseModel, Field


class CompactedFile(BaseModel):
    path: str
    state: str


class CompactionSummary(BaseModel):
    goal: str = Field(min_length=1)
    completed: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    files: list[CompactedFile] = Field(default_factory=list)
    todos: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    critical_data: list[str] = Field(default_factory=list)

    # 将结构化摘要渲染成供模型续接和人工审计的 Markdown
    def to_markdown(self) -> str:
        sections = [f"## Goal\n{self.goal}"]
        sections.append(_render_list("Completed", self.completed))
        sections.append(_render_list("Constraints", self.constraints))
        sections.append(_render_list("Decisions", self.decisions))
        file_lines = [f"`{item.path}`: {item.state}" for item in self.files]
        sections.append(_render_list("Files", file_lines))
        sections.append(_render_list("TODO", self.todos))
        sections.append(_render_list("Errors", self.errors))
        sections.append(_render_list("Critical Data", self.critical_data))
        return "\n\n".join(sections)


class CompactionQuality(BaseModel):
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    checks: dict[str, bool]
    missing: list[str] = Field(default_factory=list)


# 将字符串列表渲染为稳定的 Markdown 小节
def _render_list(title: str, values: list[str]) -> str:
    body = "\n".join(f"- {value}" for value in values) if values else "- None"
    return f"## {title}\n{body}"

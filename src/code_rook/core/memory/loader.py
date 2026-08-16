from __future__ import annotations

from pathlib import Path

# 项目级指令文件候选，先行业标准后 CodeRook 专属，顺序即展示顺序
_PROJECT_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


# 读取指定路径的 context.md，路径不存在或内容为空时返回空字符串
def load_context_file(path: Path) -> str:
    p = path.expanduser()
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8").strip()


# 按顺序加载项目级指令：AGENTS.md / CLAUDE.md（行业标准）+ .coderook/context.md（专属覆盖）
def load_project_instructions(workspace: Path | None = None) -> str:
    base = (workspace or Path.cwd()).expanduser()
    sections: list[str] = []
    for name in _PROJECT_INSTRUCTION_FILES:
        content = load_context_file(base / name)
        if content:
            sections.append(f"### From {name}\n\n{content}")
    coderook_ctx = load_context_file(base / ".coderook" / "context.md")
    if coderook_ctx:
        sections.append("### From .coderook/context.md\n\n" + coderook_ctx)
    return "\n\n".join(sections).strip()

from __future__ import annotations

import logging
from pathlib import Path

# 项目级指令文件候选，先行业标准后 CodeRook 专属，顺序即展示顺序
_PROJECT_INSTRUCTION_FILES = (
    "AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD",
)
_LOGGER = logging.getLogger(__name__)


# 读取指定路径的 context.md，路径不存在或内容为空时返回空字符串
def load_context_file(path: Path) -> str:
    p = path.expanduser()
    if not p.is_file():
        return ""
    return p.read_text(encoding="utf-8-sig").strip()


# 按 Pi 优先级选择本层唯一指令文件，目录及无法读取的候选不遮蔽后续文件。
def _directory_instruction(directory: Path) -> tuple[Path, str] | None:
    for name in _PROJECT_INSTRUCTION_FILES:
        path = directory / name
        try:
            if path.is_file():
                return path, load_context_file(path)
        except (OSError, UnicodeError):
            _LOGGER.warning("Cannot read context file %s", path, exc_info=True)
    return None


# 找出嵌套 linked worktree 自有指令遮蔽的主仓库同名文件，普通仓库不受影响。
def _shadowed_instruction(base: Path) -> Path | None:
    for directory in (base, *base.parents):
        git_path = directory / ".git"
        if git_path.is_dir():
            return None
        if not git_path.is_file():
            continue
        try:
            marker = git_path.read_text(encoding="utf-8").strip()
            if not marker.startswith("gitdir:"):
                return None
            git_dir = (directory / marker.removeprefix("gitdir:").strip()).resolve()
            common_file = git_dir / "commondir"
            if not common_file.is_file():
                return None
            common_dir = (git_dir / common_file.read_text(encoding="utf-8").strip()).resolve()
            main_root = common_dir.parent
            if directory == main_root or not directory.is_relative_to(main_root):
                return None
            if (main_root / ".git").resolve() != common_dir:
                return None
            own = _directory_instruction(directory)
            return (main_root / own[0].name).resolve() if own else None
        except (OSError, UnicodeError):
            _LOGGER.warning("Cannot inspect worktree context at %s", directory, exc_info=True)
            return None
    return None


# 从最外层祖先到工作目录加载指令，同层只取首选文件并保留 CodeRook 补充上下文。
def load_project_instructions(workspace: Path | None = None) -> str:
    base = (workspace or Path.cwd()).expanduser().resolve()
    sections: list[str] = []
    shadowed = _shadowed_instruction(base)
    seen: set[Path] = set()
    for directory in (*reversed(base.parents), base):
        selected = _directory_instruction(directory)
        if selected is None:
            continue
        path, content = selected
        canonical = path.resolve()
        if canonical == shadowed or canonical in seen:
            continue
        seen.add(canonical)
        if content:
            label = path.name if directory == base else path.as_posix()
            sections.append(f"### From {label}\n\n{content}")
    coderook_ctx = load_context_file(base / ".coderook" / "context.md")
    if coderook_ctx:
        sections.append("### From .coderook/context.md\n\n" + coderook_ctx)
    return "\n\n".join(sections).strip()


# 加载用户级首选指令及旧 context.md，供所有项目共享。
def load_global_instructions(agent_dir: Path | None = None) -> str:
    directory = (agent_dir or Path("~/.coderook")).expanduser().resolve()
    selected = _directory_instruction(directory)
    sections: list[str] = []
    if selected and selected[1]:
        sections.append(f"### From {selected[0].as_posix()}\n\n{selected[1]}")
    legacy = load_context_file(directory / "context.md")
    if legacy:
        sections.append(legacy)
    return "\n\n".join(sections)


# 按受信项目优先、用户目录其次选择系统提示文件，替换与追加分别解析。
def load_system_prompt_files(
    workspace: Path,
    *,
    workspace_trusted: bool,
    agent_dir: Path | None = None,
) -> tuple[str | None, str]:
    directories = [(agent_dir or Path("~/.coderook")).expanduser()]
    if workspace_trusted:
        directories.insert(0, workspace.expanduser() / ".coderook")
    values: list[str | None] = []
    for name in ("SYSTEM.md", "APPEND_SYSTEM.md"):
        value = None
        for directory in directories:
            path = directory / name
            if path.is_file():
                value = load_context_file(path)
                break
        values.append(value)
    return values[0], values[1] or ""

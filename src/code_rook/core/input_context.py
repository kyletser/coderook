from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

_REFERENCE_SUFFIX = (
    "Bounded file references selected by the user: {references}. "
    "Read only the ranges needed for this task; do not inject entire files by default."
)


# 从用户可见文本中提取最多八个 @文件 标记
def extract_file_reference_tokens(visible_content: str) -> list[str]:
    return [
        token[1:].strip(".,;，。；:：")
        for token in visible_content.split()
        if token.startswith("@") and len(token) > 1
    ][:8]


# 在工作区内解析显式文件引用，拒绝越界目标并对唯一模糊匹配做补全
def resolve_file_references(
    workspace: Path,
    references: Iterable[str],
) -> list[str]:
    root = workspace.resolve()
    resolved: list[str] = []
    for raw_value in references:
        raw = raw_value.strip().removeprefix("@").strip(".,;，。；:：")
        if not raw:
            continue
        candidate = (root / raw).resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            resolved.append(relative.as_posix())
            continue
        name = Path(raw).name
        if not name:
            continue
        matches: list[Path] = []
        for path in root.rglob(f"*{name}*"):
            if ".git" in path.parts or ".coderook" in path.parts or not path.is_file():
                continue
            target = path.resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            matches.append(path)
            if len(matches) > 1:
                break
        if len(matches) == 1:
            resolved.append(matches[0].relative_to(root).as_posix())
    return list(dict.fromkeys(resolved))[:8]


# 为模型输入追加可按需读取的工作区文件清单，同时保持用户正文不变
def augment_file_references(
    content: str,
    visible_content: str,
    workspace: Path,
    *,
    explicit_references: Iterable[str] | None = None,
) -> str:
    references = (
        list(explicit_references)
        if explicit_references is not None
        else extract_file_reference_tokens(visible_content)
    )
    selected = resolve_file_references(workspace, references)
    if not selected:
        return content
    suffix = _REFERENCE_SUFFIX.format(
        references=json.dumps(selected, ensure_ascii=False),
    )
    return f"{content}\n\n{suffix}"

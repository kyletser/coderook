from __future__ import annotations

import json
from typing import Any, Literal

from code_rook.core.session.model import Session

SessionExportFormat = Literal["markdown", "json"]


def _markdown_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, indent=2)

    sections: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            sections.append(json.dumps(block, ensure_ascii=False))
            continue
        block_type = block.get("type")
        if block_type == "text":
            sections.append(str(block.get("text", "")))
        elif block_type == "tool_use":
            name = str(block.get("name", "tool"))
            payload = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
            sections.append(f"**Tool call: `{name}`**\n\n```json\n{payload}\n```")
        elif block_type == "tool_result":
            payload = json.dumps(block.get("content", ""), ensure_ascii=False, indent=2)
            sections.append(f"**Tool result**\n\n```json\n{payload}\n```")
        else:
            payload = json.dumps(block, ensure_ascii=False, indent=2)
            sections.append(f"```json\n{payload}\n```")
    return "\n\n".join(section for section in sections if section)


def export_session(
    session: Session,
    messages: list[dict[str, Any]],
    notes: str,
    export_format: SessionExportFormat,
) -> tuple[str, str, str]:
    if export_format == "json":
        content = json.dumps(
            {
                "schema_version": 1,
                "session": session.to_dict(),
                "messages": messages,
                "notes": notes,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        return f"{session.id}.json", "application/json", content

    title = session.title.strip() or session.id
    lines = [
        f"# {title}",
        "",
        f"- Session: `{session.id}`",
        f"- Status: `{session.status}`",
        f"- Created: `{session.created_at}`",
        f"- Updated: `{session.updated_at}`",
    ]
    if session.parent_session_id is not None:
        lines.append(f"- Forked from: `{session.parent_session_id}`")
    lines.extend(["", "## Conversation", ""])
    for message in messages:
        role = str(message.get("role", "unknown")).capitalize()
        lines.extend([f"### {role}", "", _markdown_content(message.get("content", "")), ""])
    if notes.strip():
        lines.extend(["## Notes", "", notes.rstrip(), ""])
    return f"{session.id}.md", "text/markdown", "\n".join(lines).rstrip() + "\n"

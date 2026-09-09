from __future__ import annotations

import html
import json
from typing import Any, Literal

from code_rook.core.session.model import Session

SessionExportFormat = Literal["markdown", "json", "html"]


# 将消息内容转换为适合 Markdown 导出的文本
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
        elif block_type in {"thinking", "reasoning"}:
            thinking = str(block.get("thinking", block.get("text", ""))).strip()
            quoted = "\n".join(f"> {line}" if line else ">" for line in thinking.splitlines())
            sections.append(f"**Thinking**\n\n{quoted}" if quoted else "**Thinking**")
        elif block_type == "redacted_thinking":
            sections.append("**Thinking redacted by provider**")
        elif block_type == "tool_use":
            name = str(block.get("name", "tool"))
            payload = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
            sections.append(f"**Tool call: `{name}`**\n\n```json\n{payload}\n```")
        elif block_type == "tool_result":
            payload = json.dumps(block.get("content", ""), ensure_ascii=False, indent=2)
            sections.append(f"**Tool result**\n\n```json\n{payload}\n```")
        elif block_type == "image":
            raw_source = block.get("source")
            source = raw_source if isinstance(raw_source, dict) else block
            media_type = str(
                source.get("media_type", source.get("mime_type", source.get("mimeType", "image")))
            )
            sections.append(f"**Image attachment** · `{media_type}`")
        else:
            payload = json.dumps(block, ensure_ascii=False, indent=2)
            sections.append(f"```json\n{payload}\n```")
    return "\n\n".join(section for section in sections if section)


# 将任意值转为经过 HTML 转义的可读文本
def _escaped(value: Any, *, pretty: bool = False) -> str:
    if isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, ensure_ascii=False, indent=2 if pretty else None)
    return html.escape(rendered, quote=True)


# 将单个模型内容块渲染为不执行脚本的静态 HTML
def _html_block(block: Any) -> str:
    if not isinstance(block, dict):
        return f'<pre class="content">{_escaped(block, pretty=True)}</pre>'
    block_type = str(block.get("type", ""))
    if block_type == "text":
        return f'<div class="content">{_escaped(block.get("text", ""))}</div>'
    if block_type in {"thinking", "reasoning"}:
        text = block.get("thinking", block.get("text", ""))
        return (
            '<details class="thinking"><summary>Thinking</summary>'
            f'<div class="content">{_escaped(text)}</div></details>'
        )
    if block_type == "tool_use":
        name = _escaped(block.get("name", "tool"))
        payload = _escaped(block.get("input", {}), pretty=True)
        return (
            '<details class="tool"><summary>Tool call · '
            f"{name}</summary><pre>{payload}</pre></details>"
        )
    if block_type == "tool_result":
        class_name = "tool error" if block.get("is_error") else "tool"
        payload = _escaped(block.get("content", ""), pretty=True)
        return (
            f'<details class="{class_name}"><summary>Tool result</summary>'
            f"<pre>{payload}</pre></details>"
        )
    if block_type == "image":
        raw_source = block.get("source")
        source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else block
        media_type = str(
            source.get("media_type", source.get("mime_type", source.get("mimeType", "")))
        )
        data = source.get("data", source.get("data_base64", ""))
        if media_type in {"image/png", "image/jpeg", "image/webp", "image/gif"} and isinstance(
            data, str
        ) and data:
            data_uri = html.escape(f"data:{media_type};base64,{data}", quote=True)
            return f'<figure><img src="{data_uri}" alt="Conversation image"></figure>'
        return '<div class="media-placeholder">Image attachment</div>'
    return f'<pre class="content">{_escaped(block, pretty=True)}</pre>'


# 将一条消息的混合内容渲染为静态 HTML
def _html_content(content: Any) -> str:
    if isinstance(content, list):
        return "".join(_html_block(block) for block in content)
    return f'<div class="content">{_escaped(content)}</div>'


# 生成可脱离 CodeRook 独立打开的单文件会话页面
def _html_export(session: Session, messages: list[dict[str, Any]], notes: str) -> str:
    title = session.title.strip() or session.id
    metadata = [
        ("Session", session.id),
        ("Status", session.status),
        ("Created", session.created_at),
        ("Updated", session.updated_at),
    ]
    if session.parent_session_id is not None:
        metadata.append(("Forked from", session.parent_session_id))
    metadata_html = "".join(
        f"<dt>{_escaped(label)}</dt><dd>{_escaped(value)}</dd>" for label, value in metadata
    )
    messages_html = "".join(
        '<article class="message '
        f'{html.escape(str(message.get("role", "unknown")).lower(), quote=True)}">'
        f'<header>{_escaped(str(message.get("role", "unknown")).capitalize())}</header>'
        f'{_html_content(message.get("content", ""))}</article>'
        for message in messages
    )
    notes_html = (
        '<section class="notes"><h2>Notes</h2>'
        f'<div class="content">{_escaped(notes.rstrip())}</div></section>'
        if notes.strip()
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>{_escaped(title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#faf9f7; --panel:#fff; --text:#242424;
  --muted:#6f6d68; --line:#dfddd8; --user:#f0efeb; --accent:#5a55d6; --bad:#b42318; }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.65 system-ui,sans-serif }}
main {{ width:min(860px,calc(100% - 32px)); margin:48px auto 80px }}
h1 {{ margin:0 0 8px; font-size:clamp(26px,5vw,40px); line-height:1.2 }}
.brand {{ color:var(--accent); font-size:12px; font-weight:700; letter-spacing:.12em }}
dl {{ display:grid; grid-template-columns:max-content 1fr; gap:2px 14px; color:var(--muted);
  margin:20px 0 40px; font-size:13px }}
dt {{ font-weight:650 }} dd {{ margin:0; overflow-wrap:anywhere }}
.message {{ margin:0 0 22px; padding:18px 20px; border:1px solid var(--line);
  border-radius:14px; background:var(--panel) }}
.message.user {{ margin-left:min(18%,120px); background:var(--user) }}
.message header {{ margin-bottom:8px; color:var(--muted); font-size:12px; font-weight:700;
  text-transform:uppercase; letter-spacing:.08em }}
.content,pre {{ white-space:pre-wrap; overflow-wrap:anywhere }}
pre {{ margin:10px 0 0; padding:12px; overflow:auto; border-radius:9px; background:var(--bg);
  font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace }}
details {{ margin-top:10px; border-left:2px solid var(--line); padding-left:12px }}
summary {{ cursor:pointer; color:var(--muted); font-weight:600 }}
.error {{ border-color:var(--bad) }} .error summary {{ color:var(--bad) }}
figure {{ margin:12px 0 }} img {{ max-width:100%; max-height:560px; border-radius:10px }}
.media-placeholder {{ color:var(--muted); font-style:italic }}
.notes {{ margin-top:36px; border-top:1px solid var(--line); padding-top:20px }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#171716; --panel:#20201f; --text:#eee;
  --muted:#aaa7a0; --line:#3c3b38; --user:#292927; --accent:#aaa6ff; --bad:#ff8a80 }} }}
</style>
</head>
<body><main>
<div class="brand">CODEROOK SESSION</div>
<h1>{_escaped(title)}</h1>
<dl>{metadata_html}</dl>
<section class="conversation">{messages_html}</section>
{notes_html}
</main></body>
</html>
"""


# 按请求格式导出会话、消息和笔记
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

    if export_format == "html":
        return (
            f"{session.id}.html",
            "text/html; charset=utf-8",
            _html_export(session, messages, notes),
        )

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

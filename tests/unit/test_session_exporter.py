from __future__ import annotations

import json

from code_rook.core.session.exporter import export_session
from code_rook.core.session.model import Session


def _session() -> Session:
    return Session(
        "sess-export",
        "chat",
        "waiting_for_input",
        "Export title",
        "2026-01-01",
        "2026-01-02",
        ["run-1"],
        parent_session_id="sess-parent",
    )


def test_markdown_export_preserves_text_tools_notes_and_lineage() -> None:
    messages = [
        {"role": "user", "content": "Inspect the repo"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "name": "read_file", "input": {"path": "README.md"}},
            ],
        },
    ]

    filename, media_type, content = export_session(
        _session(),
        messages,
        "## Note\nKeep this.",
        "markdown",
    )

    assert filename == "sess-export.md"
    assert media_type == "text/markdown"
    assert "# Export title" in content
    assert "Forked from: `sess-parent`" in content
    assert "Inspect the repo" in content
    assert "Tool call: `read_file`" in content
    assert "Keep this." in content


def test_json_export_is_structured_and_roundtrips_unicode() -> None:
    filename, media_type, content = export_session(
        _session(),
        [{"role": "user", "content": "你好"}],
        "笔记",
        "json",
    )
    payload = json.loads(content)

    assert filename == "sess-export.json"
    assert media_type == "application/json"
    assert payload["schema_version"] == 1
    assert payload["session"]["parent_session_id"] == "sess-parent"
    assert payload["messages"][0]["content"] == "你好"
    assert payload["notes"] == "笔记"

from __future__ import annotations

from collections import Counter
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.workspace import WorkspaceBoundary, WorkspaceBoundaryError

_MAX_MESSAGE_CHARS = 500
DiagnosticSeverity = Literal["error", "warning", "information"]


class Diagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    severity: DiagnosticSeverity
    message: str
    rule: str = ""


class DiagnosticsReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok", "unavailable", "failed", "timeout", "truncated"]
    tool: str = ""
    diagnostics: tuple[Diagnostic, ...] = ()
    truncated: bool = False
    error: str = ""

    # 将错误诊断渲染为下一模型 step 使用的有界 transient context
    def render_context(self) -> str:
        if not self.diagnostics:
            return ""
        title = f"## Transient Diagnostics ({self.tool})" if self.tool else (
            "## Transient Diagnostics"
        )
        lines = [title]
        for item in self.diagnostics:
            rule = f" [{item.rule}]" if item.rule else ""
            lines.append(
                f"- {item.path}:{item.line}:{item.column}{rule} {item.message}"
            )
        if self.truncated:
            lines.append("- Additional diagnostics were omitted by the configured limit.")
        return "\n".join(lines)


# 将 Pyright 的零基坐标安全转换为最小为一的展示坐标
def _display_position(value: object) -> int:
    try:
        return max(1, int(str(value)) + 1)
    except (TypeError, ValueError, OverflowError):
        return 1


# 将 Pyright JSON 诊断裁剪为工作区内、按文件有界且默认仅 error 的结构
def parse_pyright_diagnostics(
    payload: dict[str, Any],
    boundary: WorkspaceBoundary,
    *,
    severities: frozenset[str] = frozenset({"error"}),
    max_per_file: int = 20,
    max_total: int = 100,
) -> tuple[tuple[Diagnostic, ...], bool]:
    raw_items = payload.get("generalDiagnostics", [])
    if not isinstance(raw_items, list):
        return (), False
    counts: Counter[str] = Counter()
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[object, ...]] = set()
    truncated = False
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        severity = str(raw.get("severity", "")).casefold()
        if severity not in severities or severity not in {
            "error",
            "warning",
            "information",
        }:
            continue
        try:
            path = boundary.resolve(str(raw.get("file", "")))
            relative = path.relative_to(boundary.root).as_posix()
        except (WorkspaceBoundaryError, OSError, RuntimeError, ValueError):
            continue
        if counts[relative] >= max_per_file or len(diagnostics) >= max_total:
            truncated = True
            continue
        raw_range = raw.get("range", {})
        range_value = raw_range if isinstance(raw_range, dict) else {}
        raw_start = range_value.get("start", {})
        start = raw_start if isinstance(raw_start, dict) else {}
        message = " ".join(str(raw.get("message", "")).split())
        if len(message) > _MAX_MESSAGE_CHARS:
            message = message[: _MAX_MESSAGE_CHARS - 3] + "..."
            truncated = True
        diagnostic = Diagnostic(
            path=relative,
            line=_display_position(start.get("line", 0)),
            column=_display_position(start.get("character", 0)),
            severity=cast(DiagnosticSeverity, severity),
            message=message,
            rule=str(raw.get("rule", "") or ""),
        )
        key = (
            diagnostic.path,
            diagnostic.line,
            diagnostic.column,
            diagnostic.severity,
            diagnostic.message,
            diagnostic.rule,
        )
        if key in seen:
            continue
        seen.add(key)
        diagnostics.append(diagnostic)
        counts[relative] += 1
    return tuple(diagnostics), truncated

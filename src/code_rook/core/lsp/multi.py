from __future__ import annotations

import re
import shutil

from code_rook.core.lsp.client import (
    PythonDiagnosticsClient,
    _run_bounded_command,
)
from code_rook.core.lsp.diagnostics import (
    Diagnostic,
    DiagnosticsReport,
)
from code_rook.core.workspace import WorkspaceBoundary

_TSC_TIMEOUT_S = 30.0
_TSC_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_TSC_LINE_RE = re.compile(
    r"^(.+?)\((\d+),(\d+)\): (error|warning) (TS\d+): (.*)$"
)
_MAX_PER_LANGUAGE = 100


# 解析 tsc --pretty false 的行式输出并过滤到指定文件集合
def parse_tsc_output(
    output: str,
    boundary: WorkspaceBoundary,
    edited_paths: frozenset[str],
    *,
    max_total: int = _MAX_PER_LANGUAGE,
) -> tuple[tuple[Diagnostic, ...], bool]:
    diagnostics: list[Diagnostic] = []
    truncated = False
    for line in output.splitlines():
        match = _TSC_LINE_RE.match(line.strip())
        if match is None:
            continue
        raw_path, line_no, column, severity, rule, message = match.groups()
        try:
            path = boundary.resolve(raw_path)
            relative = path.relative_to(boundary.root).as_posix()
        except Exception:
            continue
        if relative not in edited_paths:
            continue
        if len(diagnostics) >= max_total:
            truncated = True
            break
        diagnostics.append(
            Diagnostic(
                path=relative,
                line=max(1, int(line_no)),
                column=max(1, int(column)),
                severity=severity,  # type: ignore[arg-type]
                message=message[:500],
                rule=rule,
            )
        )
    return tuple(diagnostics), truncated


class TscDiagnosticsClient:
    # 探测 tsc（优先全局，其次 npx --no-install）并保存超时与输出上限
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        timeout_s: float = _TSC_TIMEOUT_S,
        max_output_bytes: int = _TSC_MAX_OUTPUT_BYTES,
    ) -> None:
        self._boundary = boundary
        self._tsc = shutil.which("tsc")
        self._npx = shutil.which("npx")
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes

    # 返回实际可用的 TypeScript 诊断命令名，缺失时为空
    @property
    def available(self) -> bool:
        return self._tsc is not None or self._npx is not None

    # 返回诊断工具的展示名
    @property
    def tool_name(self) -> str:
        return "tsc"

    # 对编辑过的 TS/TSX 文件执行项目级 tsc 检查并过滤出相关诊断
    async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
        if not self.available:
            return DiagnosticsReport(status="unavailable", tool=self.tool_name)
        relative_paths: list[str] = []
        for raw_path in dict.fromkeys(paths):
            if not raw_path.casefold().endswith((".ts", ".tsx")):
                continue
            try:
                path = self._boundary.resolve(raw_path)
                relative_paths.append(
                    path.relative_to(self._boundary.root).as_posix()
                )
            except Exception:
                continue
        if not relative_paths:
            return DiagnosticsReport(status="ok", tool=self.tool_name)
        if self._tsc is not None:
            argv = [self._tsc, "--noEmit", "--pretty", "false"]
        else:
            argv = [str(self._npx), "--no-install", "tsc", "--noEmit", "--pretty", "false"]
        try:
            output = await _run_bounded_command(
                argv[0],
                argv[1:],
                self._boundary.root,
                timeout_s=self._timeout_s,
                max_output_bytes=self._max_output_bytes,
            )
        except TimeoutError:
            return DiagnosticsReport(
                status="timeout",
                tool=self.tool_name,
                error=f"tsc exceeded {self._timeout_s:g}s timeout",
            )
        except (OSError, RuntimeError) as exc:
            return DiagnosticsReport(
                status="failed",
                tool=self.tool_name,
                error=str(exc)[:500],
            )
        if output.truncated:
            return DiagnosticsReport(
                status="truncated",
                tool=self.tool_name,
                truncated=True,
                error=f"tsc output exceeded {self._max_output_bytes} bytes",
            )
        # tsc 有错误时返回码非零属正常，以解析结果为准
        diagnostics, truncated = parse_tsc_output(
            output.stdout,
            self._boundary,
            frozenset(relative_paths),
        )
        return DiagnosticsReport(
            status="ok",
            tool=self.tool_name,
            diagnostics=diagnostics,
            truncated=truncated,
        )


class WorkspaceDiagnosticsClient:
    # 按语言组合诊断客户端：.py 走 pyright，.ts/.tsx 走 tsc
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        python_client: PythonDiagnosticsClient | None = None,
        tsc_client: TscDiagnosticsClient | None = None,
    ) -> None:
        self._python = python_client or PythonDiagnosticsClient(boundary)
        self._tsc = tsc_client or TscDiagnosticsClient(boundary)

    # 返回组合诊断工具的展示名
    @property
    def tool_name(self) -> str:
        return "+".join(
            name
            for name in (self._python.tool_name, self._tsc.tool_name)
            if name
        )

    # 按扩展名把文件分派给各语言客户端并合并为一份报告
    async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
        python_paths = [
            path for path in paths if path.casefold().endswith(".py")
        ]
        tsc_paths = [
            path
            for path in paths
            if path.casefold().endswith((".ts", ".tsx"))
        ]
        reports: list[DiagnosticsReport] = []
        if python_paths:
            reports.append(await self._python.diagnose(python_paths))
        if tsc_paths and self._tsc.available:
            reports.append(await self._tsc.diagnose(tsc_paths))
        if not reports:
            return DiagnosticsReport(status="ok", tool=self.tool_name)
        if len(reports) == 1:
            report = reports[0]
            if report.tool:
                return report
            return report.model_copy(update={"tool": self.tool_name})
        merged: list[Diagnostic] = []
        truncated = False
        error_parts: list[str] = []
        status = "ok"
        for report in reports:
            merged.extend(report.diagnostics)
            truncated = truncated or report.truncated
            if report.error:
                error_parts.append(f"{report.tool}: {report.error}")
            if report.status in {"failed", "timeout", "truncated"}:
                status = report.status
        combined_tool = "+".join(
            report.tool for report in reports if report.tool
        ) or self.tool_name
        return DiagnosticsReport(
            status=status,  # type: ignore[arg-type]
            tool=combined_tool,
            diagnostics=tuple(merged[:_MAX_PER_LANGUAGE]),
            truncated=truncated or len(merged) > _MAX_PER_LANGUAGE,
            error="; ".join(error_parts)[:500],
        )

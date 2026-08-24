from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import Awaitable

from code_rook.core.lsp.client import (
    PythonDiagnosticsClient,
    _run_bounded_command,
)
from code_rook.core.lsp.diagnostics import (
    Diagnostic,
    DiagnosticsReport,
)
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.workspace import WorkspaceBoundary

_TSC_TIMEOUT_S = 30.0
_TSC_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_TSC_LINE_RE = re.compile(r"^(.+?)\((\d+),(\d+)\): (error|warning) (TS\d+): (.*)$")
_MAX_PER_LANGUAGE = 100
_DEFAULT_DEBOUNCE_S = 0.05
_STATUS_PRIORITY = {
    "ok": 0,
    "unavailable": 1,
    "truncated": 2,
    "timeout": 3,
    "failed": 4,
}


# 解析 TypeScript Diagnostics 行式输出并过滤到指定文件集合
def parse_typescript_diagnostics(
    output: str,
    boundary: WorkspaceBoundary,
    edited_paths: frozenset[str],
    *,
    max_total: int = _MAX_PER_LANGUAGE,
) -> tuple[tuple[Diagnostic, ...], bool]:
    diagnostics: list[Diagnostic] = []
    seen: set[tuple[object, ...]] = set()
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
        diagnostic = Diagnostic(
            path=relative,
            line=max(1, int(line_no)),
            column=max(1, int(column)),
            severity=severity,  # type: ignore[arg-type]
            message=message[:500],
            rule=rule,
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
    return tuple(diagnostics), truncated


class TypeScriptDiagnosticsClient:
    # 探测 TypeScript Diagnostics 后端并保存超时与输出上限
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        timeout_s: float = _TSC_TIMEOUT_S,
        max_output_bytes: int = _TSC_MAX_OUTPUT_BYTES,
        process_supervisor: ProcessSupervisor | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self._boundary = boundary
        self._tsc = shutil.which("tsc")
        self._npx = shutil.which("npx")
        self._timeout_s = timeout_s
        self._max_output_bytes = max_output_bytes
        self._process_supervisor = process_supervisor

    # 返回实际可用的 TypeScript 诊断命令名，缺失时为空
    @property
    def available(self) -> bool:
        return self._tsc is not None or self._npx is not None

    # 返回诊断工具的展示名
    @property
    def tool_name(self) -> str:
        return "typescript-diagnostics"

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
                relative_paths.append(path.relative_to(self._boundary.root).as_posix())
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
                process_supervisor=self._process_supervisor,
            )
        except TimeoutError:
            return DiagnosticsReport(
                status="timeout",
                tool=self.tool_name,
                error=f"TypeScript Diagnostics exceeded {self._timeout_s:g}s timeout",
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
                error=(f"TypeScript Diagnostics output exceeded {self._max_output_bytes} bytes"),
            )
        # tsc 有错误时返回码非零属正常，以解析结果为准
        diagnostics, truncated = parse_typescript_diagnostics(
            output.stdout,
            self._boundary,
            frozenset(relative_paths),
        )
        if output.returncode != 0 and not diagnostics:
            return DiagnosticsReport(
                status="failed",
                tool=self.tool_name,
                error=("TypeScript Diagnostics exited non-zero without any parseable diagnostics"),
            )
        return DiagnosticsReport(
            status="ok",
            tool=self.tool_name,
            diagnostics=diagnostics,
            truncated=truncated,
        )


# 保留旧解析函数名称，调用方获得完全相同的 Diagnostics 语义
def parse_tsc_output(
    output: str,
    boundary: WorkspaceBoundary,
    edited_paths: frozenset[str],
    *,
    max_total: int = _MAX_PER_LANGUAGE,
) -> tuple[tuple[Diagnostic, ...], bool]:
    return parse_typescript_diagnostics(
        output,
        boundary,
        edited_paths,
        max_total=max_total,
    )


TscDiagnosticsClient = TypeScriptDiagnosticsClient


class WorkspaceDiagnosticsClient:
    # 按语言组合 Diagnostics 客户端并初始化异步去抖状态
    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        python_client: PythonDiagnosticsClient | None = None,
        typescript_client: TypeScriptDiagnosticsClient | None = None,
        tsc_client: TypeScriptDiagnosticsClient | None = None,
        process_supervisor: ProcessSupervisor | None = None,
        debounce_s: float = _DEFAULT_DEBOUNCE_S,
    ) -> None:
        if typescript_client is not None and tsc_client is not None:
            raise ValueError("set either typescript_client or legacy tsc_client, not both")
        if debounce_s < 0:
            raise ValueError("debounce_s must not be negative")
        self._python = python_client or PythonDiagnosticsClient(
            boundary,
            process_supervisor=process_supervisor,
        )
        self._tsc = (
            typescript_client
            or tsc_client
            or TypeScriptDiagnosticsClient(
                boundary,
                process_supervisor=process_supervisor,
            )
        )
        self._debounce_s = debounce_s
        self._debounce_lock = asyncio.Lock()
        self._pending_paths: set[str] = set()
        self._pending_waiters: list[asyncio.Future[DiagnosticsReport]] = []
        self._debounce_task: asyncio.Task[None] | None = None

    # 返回组合诊断工具的展示名
    @property
    def tool_name(self) -> str:
        return "+".join(name for name in (self._python.tool_name, self._tsc.tool_name) if name)

    # 合并短时间内并发编辑路径，让所有调用者共享同一次 Diagnostics 结果
    async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[DiagnosticsReport] = loop.create_future()
        async with self._debounce_lock:
            self._pending_paths.update(paths)
            self._pending_waiters.append(waiter)
            current = self._debounce_task
            if current is not None and not current.done():
                current.cancel()
            self._debounce_task = asyncio.create_task(
                self._flush_debounced(),
                name="coderook-workspace-diagnostics-debounce",
            )
        return await waiter

    # 等待静默窗口后取走当前批次，并把基础设施异常转换为失败报告
    async def _flush_debounced(self) -> None:
        try:
            await asyncio.sleep(self._debounce_s)
        except asyncio.CancelledError:
            return
        async with self._debounce_lock:
            paths = sorted(
                self._pending_paths,
                key=lambda value: (value.casefold(), value),
            )
            waiters = self._pending_waiters
            self._pending_paths = set()
            self._pending_waiters = []
            self._debounce_task = None
        try:
            report = await self._diagnose_now(paths)
        except Exception as exc:
            report = DiagnosticsReport(
                status="failed",
                tool=self.tool_name,
                error=f"Diagnostics infrastructure failed: {exc}"[:500],
            )
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(report)

    # 按扩展名分派 Python 与 TypeScript Diagnostics 并合并结构化报告
    async def _diagnose_now(self, paths: list[str]) -> DiagnosticsReport:
        python_paths = [path for path in paths if path.casefold().endswith(".py")]
        tsc_paths = [path for path in paths if path.casefold().endswith((".ts", ".tsx"))]
        pending: list[Awaitable[DiagnosticsReport]] = []
        if python_paths:
            pending.append(self._python.diagnose(python_paths))
        if tsc_paths:
            pending.append(self._tsc.diagnose(tsc_paths))
        reports = list(await asyncio.gather(*pending)) if pending else []
        if not reports:
            return DiagnosticsReport(status="ok", tool=self.tool_name)
        if len(reports) == 1:
            report = reports[0]
            if report.tool:
                return report
            return report.model_copy(update={"tool": self.tool_name})
        merged: list[Diagnostic] = []
        seen: set[tuple[object, ...]] = set()
        truncated = False
        error_parts: list[str] = []
        status = "ok"
        for report in reports:
            for diagnostic in report.diagnostics:
                key = (
                    diagnostic.path,
                    diagnostic.line,
                    diagnostic.column,
                    diagnostic.severity,
                    diagnostic.message,
                    diagnostic.rule,
                )
                if key not in seen:
                    seen.add(key)
                    merged.append(diagnostic)
            truncated = truncated or report.truncated
            if report.error:
                error_parts.append(f"{report.tool}: {report.error}")
            if _STATUS_PRIORITY[report.status] > _STATUS_PRIORITY[status]:
                status = report.status
        combined_tool = "+".join(report.tool for report in reports if report.tool) or self.tool_name
        return DiagnosticsReport(
            status=status,  # type: ignore[arg-type]
            tool=combined_tool,
            diagnostics=tuple(merged[:_MAX_PER_LANGUAGE]),
            truncated=truncated or len(merged) > _MAX_PER_LANGUAGE,
            error="; ".join(error_parts)[:500],
        )

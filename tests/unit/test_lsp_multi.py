from __future__ import annotations

from pathlib import Path

from code_rook.core.lsp.client import PythonDiagnosticsClient
from code_rook.core.lsp.diagnostics import DiagnosticsReport
from code_rook.core.lsp.multi import (
    TscDiagnosticsClient,
    WorkspaceDiagnosticsClient,
    parse_tsc_output,
)
from code_rook.core.workspace import WorkspaceBoundary

_TSC_OUTPUT = """src/app.ts(12,5): error TS2304: Cannot find name 'userr'.
src/app.ts(30,1): warning TS6133: 'x' is declared but its value is never read.
src/other.ts(7,9): error TS2322: Type 'string' is not assignable to type 'number'.
src/app.ts:3:1 - error TS1005: semicolon expected.
no-match line
"""


# 功能：验证 tsc 行式输出解析只保留被编辑文件且规则号入档
# 设计：构造混合 error/warning/无关文件与噪音行，断言过滤、坐标与规则字段
def test_parse_tsc_output_filters_to_edited_files(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)

    diagnostics, truncated = parse_tsc_output(
        _TSC_OUTPUT,
        boundary,
        frozenset({"src/app.ts"}),
    )

    assert not truncated
    assert len(diagnostics) == 2
    first, second = diagnostics
    assert first.path == "src/app.ts" and first.line == 12 and first.column == 5
    assert first.severity == "error" and first.rule == "TS2304"
    assert "userr" in first.message
    assert second.severity == "warning" and second.rule == "TS6133"


# 功能：验证 tsc 不可用时报告 unavailable 而不报错
# 设计：注入空命令探测结果，直接调用 diagnose 断言降级状态
async def test_tsc_client_unavailable_when_binary_missing(tmp_path: Path) -> None:
    client = TscDiagnosticsClient(WorkspaceBoundary(tmp_path))
    client._tsc = None
    client._npx = None

    report = await client.diagnose(["src/a.ts"])

    assert report.status == "unavailable"
    assert report.tool == "tsc"


# 功能：验证工作区客户端按扩展名分派并合并两种语言的诊断
# 设计：stub 两个语言客户端返回固定报告，混合 .py/.ts 路径断言合并结果与工具名
async def test_workspace_client_dispatches_by_extension(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)

    class _StubPython(PythonDiagnosticsClient):
        # 记录收到的路径并返回一条 Python 错误
        def __init__(self) -> None:
            super().__init__(boundary)
            self.seen: list[str] = []

        async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
            from code_rook.core.lsp.diagnostics import Diagnostic

            self.seen = list(paths)
            return DiagnosticsReport(
                status="ok",
                tool="pyright",
                diagnostics=(
                    Diagnostic(
                        path="a.py", line=3, column=1, severity="error", message="bad"
                    ),
                ),
            )

    class _StubTsc(TscDiagnosticsClient):
        # 记录收到的路径并返回一条 TS 错误
        def __init__(self) -> None:
            super().__init__(boundary)
            self.seen: list[str] = []

        async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
            from code_rook.core.lsp.diagnostics import Diagnostic

            self.seen = list(paths)
            return DiagnosticsReport(
                status="ok",
                tool="tsc",
                diagnostics=(
                    Diagnostic(
                        path="a.ts",
                        line=9,
                        column=2,
                        severity="error",
                        message="TS2304",
                        rule="TS2304",
                    ),
                ),
            )

    python_stub = _StubPython()
    tsc_stub = _StubTsc()
    client = WorkspaceDiagnosticsClient(
        boundary,
        python_client=python_stub,  # type: ignore[arg-type]
        tsc_client=tsc_stub,
    )

    report = await client.diagnose(["a.py", "a.ts", "b.ts"])

    assert python_stub.seen == ["a.py"]
    assert tsc_stub.seen == ["a.ts", "b.ts"]
    assert report.status == "ok"
    assert {item.path for item in report.diagnostics} == {"a.py", "a.ts"}
    assert "pyright" in report.tool and "tsc" in report.tool
    assert "Transient Diagnostics" in report.render_context()


# 功能：验证仅一种语言被触发时透传其报告并补齐组合工具名
# 设计：stub Python 返回无工具名报告，断言工具名被填充且无 tsc 调用
async def test_workspace_client_single_language_passthrough(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)

    class _StubPython(PythonDiagnosticsClient):
        # 返回空工具名的成功报告
        def __init__(self) -> None:
            super().__init__(boundary)

        async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
            return DiagnosticsReport(status="ok", tool="pyright", diagnostics=())

    class _UnavailableTsc(TscDiagnosticsClient):
        # 标记不可用以跳过 tsc 分支
        def __init__(self) -> None:
            super().__init__(boundary)
            self._tsc = None
            self._npx = None

    client = WorkspaceDiagnosticsClient(
        boundary,
        python_client=_StubPython(),  # type: ignore[arg-type]
        tsc_client=_UnavailableTsc(),
    )

    report = await client.diagnose(["only.py"])

    assert report.status == "ok"
    assert report.tool == "pyright"

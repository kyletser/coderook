from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from code_rook.core.lsp import client as client_module
from code_rook.core.lsp.client import PythonDiagnosticsClient, _CommandOutput
from code_rook.core.lsp.diagnostics import parse_pyright_diagnostics
from code_rook.core.workspace import WorkspaceBoundary


# 构造最小 Pyright 诊断项，便于覆盖解析边界
def _diagnostic(path: Path, *, severity: str = "error", index: object = 0) -> dict[str, object]:
    return {
        "file": str(path),
        "severity": severity,
        "message": "  broken   assignment  ",
        "rule": "reportAssignmentType",
        "range": {"start": {"line": index, "character": index}},
    }


# 功能：验证 Pyright 输出只保留工作区内 error，并转换为一基坐标
# 设计：混合 warning、越界路径和非法坐标，覆盖默认过滤及坏数据安全降级
def test_parse_pyright_diagnostics_filters_and_bounds(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    target = tmp_path / "pkg" / "sample.py"
    payload = {
        "generalDiagnostics": [
            _diagnostic(target, index=2),
            _diagnostic(target, severity="warning"),
            _diagnostic(tmp_path.parent / "outside.py"),
            _diagnostic(target, index="invalid"),
        ]
    }

    diagnostics, truncated = parse_pyright_diagnostics(payload, boundary)

    assert not truncated
    assert len(diagnostics) == 2
    assert diagnostics[0].path == "pkg/sample.py"
    assert (diagnostics[0].line, diagnostics[0].column) == (3, 3)
    assert (diagnostics[1].line, diagnostics[1].column) == (1, 1)
    assert diagnostics[0].message == "broken assignment"


# 功能：验证 Python Diagnostics 对 Unicode 路径保持工作区相对原文
# 设计：构造中文目录和文件名的 Pyright JSON，直接检查解析结果没有转义或替换字符
def test_parse_pyright_diagnostics_preserves_unicode_path(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    target = tmp_path / "模块" / "样例.py"

    diagnostics, truncated = parse_pyright_diagnostics(
        {"generalDiagnostics": [_diagnostic(target)]},
        boundary,
    )

    assert truncated is False
    assert diagnostics[0].path == "模块/样例.py"


# 功能：验证单文件与全局数量边界会裁剪诊断并标记 truncated
# 设计：同文件写入三条 error 且限制为一条，直接命中最小裁剪分支
def test_parse_pyright_diagnostics_marks_limit_truncation(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    target = tmp_path / "sample.py"
    payload = {"generalDiagnostics": [_diagnostic(target) for _ in range(3)]}

    diagnostics, truncated = parse_pyright_diagnostics(
        payload,
        boundary,
        max_per_file=1,
        max_total=2,
    )

    assert len(diagnostics) == 1
    assert truncated


# 功能：验证未安装 basedpyright/pyright 时诊断客户端无异常降级
# 设计：屏蔽 PATH 探测后调用 Python 文件诊断，断言结构化 unavailable 状态
async def test_client_degrades_when_pyright_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    client = PythonDiagnosticsClient(WorkspaceBoundary(tmp_path))

    report = await client.diagnose(["sample.py"])

    assert report.status == "unavailable"
    assert report.diagnostics == ()


# 功能：验证诊断命令超时会转换为有界状态而不打断 agent loop
# 设计：替换底层命令为立即抛 TimeoutError 的协程，隔离真实子进程与时钟
async def test_client_converts_timeout_to_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 模拟底层进程超时
    async def _timeout(*args: object, **kwargs: object) -> _CommandOutput:
        raise TimeoutError

    monkeypatch.setattr(client_module, "_run_bounded_command", _timeout)
    client = PythonDiagnosticsClient(
        WorkspaceBoundary(tmp_path),
        executable="pyright",
        timeout_s=0.1,
    )

    report = await client.diagnose(["sample.py"])

    assert report.status == "timeout"
    assert "0.1s" in report.error


# 功能：验证巨大输出和无效 JSON 均以失败收据返回
# 设计：参数化底层输出覆盖 truncated 优先分支与 JSON 解析分支，不依赖真实 Pyright
@pytest.mark.parametrize(
    ("output", "expected_status"),
    [
        (_CommandOutput(0, "too much", True), "truncated"),
        (_CommandOutput(0, "not json", False), "failed"),
    ],
)
async def test_client_degrades_for_bounded_output_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: _CommandOutput,
    expected_status: str,
) -> None:
    # 返回参数化命令输出
    async def _output(*args: object, **kwargs: object) -> _CommandOutput:
        return output

    monkeypatch.setattr(client_module, "_run_bounded_command", _output)
    client = PythonDiagnosticsClient(
        WorkspaceBoundary(tmp_path),
        executable="pyright",
        max_output_bytes=16,
    )

    report = await client.diagnose(["sample.py"])

    assert report.status == expected_status


# 功能：验证有效 Pyright JSON 即使退出码非零也能返回真实错误诊断
# 设计：模拟 Pyright 发现类型错误时常见的非零退出码，确保不会误判工具故障
async def test_client_accepts_diagnostic_json_with_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "sample.py"
    payload = {"generalDiagnostics": [_diagnostic(target)]}

    # 返回包含诊断的真实 JSON 形状
    async def _output(*args: object, **kwargs: object) -> _CommandOutput:
        return _CommandOutput(1, json.dumps(payload), False)

    monkeypatch.setattr(client_module, "_run_bounded_command", _output)
    client = PythonDiagnosticsClient(
        WorkspaceBoundary(tmp_path),
        executable="pyright",
    )

    report = await client.diagnose(["sample.py"])

    assert report.status == "ok"
    assert len(report.diagnostics) == 1


# 功能：验证 Python Diagnostics 异常退出且没有诊断项时不会返回假绿色
# 设计：返回合法但空的 Pyright JSON 与退出码二，区分协议可解析和基础设施真实成功
async def test_python_diagnostics_nonzero_empty_payload_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 模拟 Pyright 基础设施异常但仍打印合法 JSON 根对象
    async def _output(*args: object, **kwargs: object) -> _CommandOutput:
        return _CommandOutput(2, '{"generalDiagnostics": []}', False)

    monkeypatch.setattr(client_module, "_run_bounded_command", _output)
    client = PythonDiagnosticsClient(
        WorkspaceBoundary(tmp_path),
        executable="pyright",
    )

    report = await client.diagnose(["sample.py"])

    assert report.status == "failed"
    assert report.infrastructure_ok is False
    assert "non-zero" in report.error
    assert "Infrastructure status `failed`" in report.render_context()

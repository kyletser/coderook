from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_crash_recovery_reports import validate_reports

_SHA = "a" * 40


# 构造聚合门禁使用的完整单平台强杀报告
def _report(platform: str) -> dict[str, object]:
    results = [
        {
            "iteration": index + 1,
            "phase": (
                "llm_request_in_flight"
                if index % 2 == 0
                else "tool_call_unresolved"
            ),
            "passed": True,
            "orphaned_tool_call_ids": [],
        }
        for index in range(100)
    ]
    return {
        "schema_version": 3,
        "commit": _SHA,
        "platform": platform,
        "architecture": "x86_64",
        "python_version": "3.12.0",
        "iterations": 100,
        "completed_iterations": 100,
        "passed": 100,
        "recovery_rate": 1.0,
        "min_rate": 0.95,
        "orphaned_tool_calls": 0,
        "infrastructure_error": None,
        "failure_context": None,
        "gate_passed": True,
        "results": results,
    }


# 写入一个平台的 JSON 报告并模拟 download-artifact 的独立目录结构
def _write_report(root: Path, platform: str, report: dict[str, object]) -> None:
    target = root / f"crash-recovery-{platform.lower()}" / "crash-recovery.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(report), encoding="utf-8")


# 功能：验证三平台各 100 轮、两类相位均衡且零孤儿时聚合门禁通过
# 设计：按 Actions 下载目录写入完整 schema 3 fixture，断言跨平台总计而非单报告结论
def test_validate_reports_accepts_complete_three_platform_matrix(tmp_path: Path) -> None:
    for platform in ("Linux", "Windows", "Darwin"):
        _write_report(tmp_path, platform, _report(platform))

    aggregate = validate_reports(tmp_path, expected_commit=_SHA)

    assert aggregate["gate_passed"] is True
    assert aggregate["errors"] == []
    assert aggregate["totals"] == {
        "reports": 3,
        "iterations": 300,
        "completed_iterations": 300,
        "passed": 300,
        "llm_phases": 150,
        "tool_phases": 150,
        "orphaned_tool_calls": 0,
    }


# 功能：验证聚合门禁拒绝提交漂移、平台缺失和任何孤儿工具调用
# 设计：同时破坏 Windows 身份与孤儿字段并省略 Darwin，确保错误列表保留全部独立根因
def test_validate_reports_rejects_incomplete_or_mismatched_evidence(
    tmp_path: Path,
) -> None:
    _write_report(tmp_path, "Linux", _report("Linux"))
    windows = _report("Windows")
    windows["commit"] = "b" * 40
    windows["orphaned_tool_calls"] = 1
    results = windows["results"]
    assert isinstance(results, list)
    assert isinstance(results[0], dict)
    assert isinstance(results[1], dict)
    results[0]["iteration"] = 2
    results[1]["orphaned_tool_call_ids"] = ["orphan-1"]
    _write_report(tmp_path, "Windows", windows)

    aggregate = validate_reports(tmp_path, expected_commit=_SHA)
    errors = "\n".join(str(item) for item in aggregate["errors"])

    assert aggregate["gate_passed"] is False
    assert "commit does not match workflow SHA" in errors
    assert "result iteration sequence mismatch" in errors
    assert "orphaned tool calls must be zero" in errors
    assert "expected 3 reports, found 2" in errors
    assert "platform set must be exactly Darwin, Linux, Windows" in errors

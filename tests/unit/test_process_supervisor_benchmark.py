from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.run_process_supervisor_benchmark import (
    ProcessSupervisorReport,
    ResourceSample,
    ResourceSummary,
    _percentile,
    build_report,
    write_report,
)


# 功能：验证资源基线使用 nearest-rank 计算小样本 P95
# 设计：用无序边界值排除插值和输入顺序影响，使远端报告可人工复算
def test_resource_percentile_uses_nearest_rank() -> None:
    assert _percentile([3.0, 1.0, 2.0, 100.0], 0.95) == 100.0


# 功能：验证真实受管子进程生成当前平台可解释的资源基线
# 设计：运行单个短固定负载，在支持采样的平台额外断言完整率和峰值内存
async def test_build_process_supervisor_report_from_real_workload() -> None:
    report = await build_report(iterations=1, duration_s=0.25, memory_mb=2)

    assert report.gate_passed is True
    assert report.iterations == 1
    assert report.samples[0].wall_time_ms > 0
    assert report.summary.process_count_p95 >= 1
    if report.complete_expected:
        assert report.summary.complete_rate == 1.0
        assert report.summary.peak_memory_p95_bytes > 0


# 功能：验证资源基线同时输出可机读 JSON 与可审阅 Markdown
# 设计：用固定模型对象写入临时目录，复读 schema、commit 和摘要状态避免格式漂移
def test_write_process_supervisor_report(tmp_path: Path) -> None:
    sample = ResourceSample(
        iteration=1,
        wall_time_ms=250,
        user_cpu_ms=100,
        system_cpu_ms=10,
        peak_memory_bytes=4096,
        process_count=1,
        samples=2,
        complete=True,
    )
    report = ProcessSupervisorReport(
        generated_at="2026-08-20T00:00:00+00:00",
        commit="abc123",
        platform="Linux",
        architecture="x86_64",
        python_version="3.12.0",
        iterations=1,
        workload_duration_s=0.25,
        workload_memory_mb=2,
        complete_expected=True,
        samples=[sample],
        summary=ResourceSummary(
            wall_time_p95_ms=250,
            user_cpu_p95_ms=100,
            system_cpu_p95_ms=10,
            peak_memory_p95_bytes=4096,
            process_count_p95=1,
            complete_rate=1,
        ),
        errors=[],
        gate_passed=True,
    )

    json_path, markdown_path = write_report(report, tmp_path / "resource.json")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert payload["schema_version"] == 1
    assert payload["commit"] == "abc123"
    assert "Status: **PASS**" in markdown


# 功能：验证空样本不能伪造百分位数
# 设计：直接覆盖无数据边界并断言显式失败，避免 CI 产出看似有效的零值报告
def test_resource_percentile_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one value"):
        _percentile([], 0.95)

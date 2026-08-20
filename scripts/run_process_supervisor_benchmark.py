from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from code_rook.core.processes import ProcessSupervisor, ProcessUsage


class ResourceSample(BaseModel):
    iteration: int = Field(ge=1)
    wall_time_ms: int = Field(ge=0)
    user_cpu_ms: int = Field(ge=0)
    system_cpu_ms: int = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    process_count: int = Field(ge=1)
    samples: int = Field(ge=0)
    complete: bool


class ResourceSummary(BaseModel):
    wall_time_p95_ms: float = Field(ge=0)
    user_cpu_p95_ms: float = Field(ge=0)
    system_cpu_p95_ms: float = Field(ge=0)
    peak_memory_p95_bytes: float = Field(ge=0)
    process_count_p95: float = Field(ge=1)
    complete_rate: float = Field(ge=0, le=1)


class ProcessSupervisorReport(BaseModel):
    schema_version: int = 1
    generated_at: str
    commit: str
    platform: str
    architecture: str
    python_version: str
    iterations: int = Field(ge=1)
    workload_duration_s: float = Field(gt=0)
    workload_memory_mb: int = Field(ge=1)
    complete_expected: bool
    samples: list[ResourceSample]
    summary: ResourceSummary
    errors: list[str]
    gate_passed: bool


# 解析三平台资源基线脚本参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure ProcessSupervisor resource collection with a fixed workload."
    )
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--duration-s", type=float, default=0.25)
    parser.add_argument("--memory-mb", type=int, default=8)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/process-supervisor.json"),
    )
    return parser.parse_args()


# 使用 nearest-rank 算法计算稳定且易复核的百分位数
def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


# 判断当前平台是否应提供完整的 CPU、内存和进程数采样
def _complete_expected() -> bool:
    if os.name == "nt":
        return True
    return os.name == "posix" and Path("/proc").is_dir()


# 读取当前提交标识，CI 优先使用不可歧义的 GITHUB_SHA
def _commit() -> str:
    github_sha = os.environ.get("GITHUB_SHA", "").strip()
    if github_sha:
        return github_sha
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 构造固定 CPU 与内存负载，确保采样窗口覆盖多个监控周期
def _workload_code(duration_s: float, memory_mb: int) -> str:
    return (
        "import time\n"
        f"payload = bytearray({memory_mb} * 1024 * 1024)\n"
        f"deadline = time.monotonic() + {duration_s!r}\n"
        "value = 0\n"
        "while time.monotonic() < deadline:\n"
        "    value = (value + sum(payload[::4096])) % 1000003\n"
        "raise SystemExit(0 if value >= 0 else 1)\n"
    )


# 运行一次真实受管子进程并返回生命周期资源快照
async def _run_sample(iteration: int, duration_s: float, memory_mb: int) -> ResourceSample:
    supervisor = ProcessSupervisor()
    process = await supervisor.start_exec(
        sys.executable,
        "-c",
        _workload_code(duration_s, memory_mb),
        label=f"resource-baseline-{iteration}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=max(10.0, duration_s * 10))
        if returncode != 0:
            raise RuntimeError(f"workload {iteration} exited with {returncode}")
        usage = supervisor.forget(process)
    finally:
        await supervisor.close()
    return _sample_from_usage(iteration, usage)


# 把 ProcessUsage 转换为带迭代编号的报告样本
def _sample_from_usage(iteration: int, usage: ProcessUsage) -> ResourceSample:
    return ResourceSample(iteration=iteration, **usage.to_dict())


# 校验样本是否满足当前平台承诺的资源采集合约
def _sample_errors(samples: list[ResourceSample], complete_expected: bool) -> list[str]:
    errors: list[str] = []
    for sample in samples:
        if sample.wall_time_ms <= 0:
            errors.append(f"iteration {sample.iteration}: wall_time_ms must be positive")
        if complete_expected and not sample.complete:
            errors.append(f"iteration {sample.iteration}: resource sample is incomplete")
        if complete_expected and sample.samples < 1:
            errors.append(f"iteration {sample.iteration}: no resource samples collected")
        if complete_expected and sample.peak_memory_bytes <= 0:
            errors.append(f"iteration {sample.iteration}: peak memory was not collected")
    return errors


# 汇总固定负载的 P95 指标与采样完整率
def _summary(samples: list[ResourceSample]) -> ResourceSummary:
    return ResourceSummary(
        wall_time_p95_ms=_percentile([float(item.wall_time_ms) for item in samples], 0.95),
        user_cpu_p95_ms=_percentile([float(item.user_cpu_ms) for item in samples], 0.95),
        system_cpu_p95_ms=_percentile([float(item.system_cpu_ms) for item in samples], 0.95),
        peak_memory_p95_bytes=_percentile(
            [float(item.peak_memory_bytes) for item in samples], 0.95
        ),
        process_count_p95=_percentile(
            [float(item.process_count) for item in samples], 0.95
        ),
        complete_rate=sum(1 for item in samples if item.complete) / len(samples),
    )


# 执行固定次数的受管负载并生成 commit-bound 资源基线报告
async def build_report(
    *, iterations: int, duration_s: float, memory_mb: int
) -> ProcessSupervisorReport:
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if memory_mb < 1:
        raise ValueError("memory_mb must be at least 1")
    samples = [
        await _run_sample(iteration, duration_s, memory_mb)
        for iteration in range(1, iterations + 1)
    ]
    complete_expected = _complete_expected()
    errors = _sample_errors(samples, complete_expected)
    return ProcessSupervisorReport(
        generated_at=datetime.now(UTC).isoformat(),
        commit=_commit(),
        platform=platform.system(),
        architecture=platform.machine(),
        python_version=platform.python_version(),
        iterations=iterations,
        workload_duration_s=duration_s,
        workload_memory_mb=memory_mb,
        complete_expected=complete_expected,
        samples=samples,
        summary=_summary(samples),
        errors=errors,
        gate_passed=not errors,
    )


# 渲染便于人工审阅的资源基线摘要
def render_markdown(report: ProcessSupervisorReport) -> str:
    summary = report.summary
    status = "PASS" if report.gate_passed else "FAIL"
    return "\n".join(
        [
            "# ProcessSupervisor Resource Baseline",
            "",
            f"- Status: **{status}**",
            f"- Commit: `{report.commit}`",
            f"- Platform: `{report.platform} {report.architecture}`",
            f"- Python: `{report.python_version}`",
            f"- Iterations: `{report.iterations}`",
            f"- Complete sampling expected: `{str(report.complete_expected).lower()}`",
            "",
            "| Metric | P95 |",
            "|---|---:|",
            f"| Wall time | {summary.wall_time_p95_ms:.0f} ms |",
            f"| User CPU | {summary.user_cpu_p95_ms:.0f} ms |",
            f"| System CPU | {summary.system_cpu_p95_ms:.0f} ms |",
            f"| Peak memory | {summary.peak_memory_p95_bytes:.0f} bytes |",
            f"| Process count | {summary.process_count_p95:.0f} |",
            f"| Complete rate | {summary.complete_rate:.1%} |",
            "",
            "Errors: " + ("none" if not report.errors else "; ".join(report.errors)),
            "",
        ]
    )


# 以 UTF-8/LF 写出 JSON 与同名 Markdown 证据文件
def write_report(report: ProcessSupervisorReport, output: Path) -> tuple[Path, Path]:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    markdown_output = output.with_suffix(".md")
    markdown_output.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return output, markdown_output


# 运行资源基线并以门禁结果决定进程退出码
async def _async_main(args: argparse.Namespace) -> int:
    report = await build_report(
        iterations=args.iterations,
        duration_s=args.duration_s,
        memory_mb=args.memory_mb,
    )
    json_path, markdown_path = write_report(report, args.output)
    print(f"resource baseline: {'PASS' if report.gate_passed else 'FAIL'}")
    print(f"json: {json_path}")
    print(f"markdown: {markdown_path}")
    return 0 if report.gate_passed else 1


# 解析命令行并启动异步资源基线入口
def main() -> int:
    return asyncio.run(_async_main(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

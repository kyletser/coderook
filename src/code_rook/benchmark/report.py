from __future__ import annotations

from pathlib import Path

from code_rook.benchmark.models import BenchmarkReport


# 将完整基准证据写为机器可读 JSON
def write_json_report(report: BenchmarkReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n")


# 将关键结果写为便于发布审阅的 Markdown 摘要
def write_markdown_report(report: BenchmarkReport, path: Path) -> None:
    summary = report.summary
    first_edit = (
        "unknown"
        if summary.first_edit_correct_rate is None
        else f"{summary.first_edit_correct_rate:.1%} ({summary.first_edit_known} known)"
    )
    cost_p50 = "unknown" if summary.cost_p50_usd is None else f"${summary.cost_p50_usd:.4f}"
    cost_p95 = "unknown" if summary.cost_p95_usd is None else f"${summary.cost_p95_usd:.4f}"
    elapsed_p50 = (
        "unknown" if summary.elapsed_p50_s is None else f"{summary.elapsed_p50_s:.1f}s"
    )
    elapsed_p95 = (
        "unknown" if summary.elapsed_p95_s is None else f"{summary.elapsed_p95_s:.1f}s"
    )
    diagnostics_p95 = (
        "unknown"
        if summary.diagnostics_p95_ms is None
        else f"{summary.diagnostics_p95_ms:.0f}ms"
    )
    process_wall_p95 = (
        "unknown"
        if summary.process_wall_p95_ms is None
        else f"{summary.process_wall_p95_ms:.0f}ms"
    )
    process_cpu_p95 = (
        "unknown"
        if summary.process_cpu_p95_ms is None
        else f"{summary.process_cpu_p95_ms:.0f}ms"
    )
    peak_memory_p95 = (
        "unknown"
        if summary.peak_memory_p95_bytes is None
        else f"{summary.peak_memory_p95_bytes / 1024 / 1024:.1f}MiB"
    )
    process_count_p95 = (
        "unknown"
        if summary.process_count_p95 is None
        else f"{summary.process_count_p95:.0f}"
    )
    process_completeness = (
        "unknown"
        if summary.process_usage_complete_rate is None
        else f"{summary.process_usage_complete_rate:.1%}"
    )
    lines = [
        "# CodeRook Benchmark Report",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Repository commit: `{report.repository_commit}`",
        f"- Route/model/wire: `{report.run_config.route_id}` / "
        f"`{report.run_config.model}` / `{report.run_config.wire_format}`",
        f"- Router/thinking/temperature: `{report.run_config.router}` / "
        f"`{report.run_config.thinking}` / `{report.run_config.temperature}`",
        f"- Config fingerprint: `{report.run_config.config_fingerprint}`",
        f"- Benchmark/dataset: `{report.run_config.benchmark_name}` / "
        f"`{report.run_config.dataset_name}` @ `{report.run_config.dataset_commit}`",
        f"- Pass@1: **{report.passed}/{report.total} ({report.pass_rate:.1%})**",
        f"- Verifier pass rate: **{summary.verifier_pass_rate:.1%}**",
        f"- First-edit correctness: **{first_edit}**",
        f"- Duration p50 / p95: **{elapsed_p50} / {elapsed_p95}**",
        f"- Cost p50 / p95: **{cost_p50} / {cost_p95}**",
        f"- Diagnostics p95: **{diagnostics_p95}**",
        "- Process wall / CPU p95: **"
        f"{process_wall_p95} / {process_cpu_p95}**",
        "- Peak memory / process count p95: **"
        f"{peak_memory_p95} / {process_count_p95}**",
        f"- Process accounting completeness: **{process_completeness}**",
        "- Non-target changes / approvals / rollbacks: **"
        f"{summary.non_target_file_changes} / {summary.approval_requests} / "
        f"{summary.rollbacks}**",
        "- Retries / compactions / daemon restarts: **"
        f"{summary.retries} / {summary.compactions} / "
        f"{summary.daemon_restarts}**",
        "",
        "| Category | Passed | Total | Pass@1 |",
        "|---|---:|---:|---:|",
    ]
    for category, category_summary in sorted(summary.categories.items()):
        lines.append(
            f"| {category} | {category_summary.passed} | {category_summary.total} | "
            f"{category_summary.pass_rate:.1%} |"
        )
    lines.extend([
        "",
        "| Task | Category | Result | Failure | Steps | Duration | Tokens | Cost |",
        "|---|---|---:|---|---:|---:|---:|---:|",
    ])
    for result in report.results:
        execution = result.execution
        tokens = execution.input_tokens + execution.output_tokens
        cost = (
            "unknown"
            if execution.estimated_cost_usd is None
            else f"${execution.estimated_cost_usd:.4f}"
        )
        lines.append(
            "| "
            f"`{result.task_id}` | {result.category} | "
            f"{'PASS' if result.passed else 'FAIL'} | {result.failure_class or '-'} | "
            f"{execution.steps} | {execution.elapsed_s:.1f}s | {tokens} | {cost} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

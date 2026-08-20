#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REQUIRED_PLATFORMS = frozenset({"Linux", "Windows", "Darwin"})
_PHASE_LLM = "llm_request_in_flight"
_PHASE_TOOL = "tool_call_unresolved"


# 解析三平台强杀报告聚合门禁参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate commit-bound crash recovery reports across platforms."
    )
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--min-rate", type=float, default=0.95)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


# 把任意值安全转换为对象列表，类型不符时返回空列表供合同报错
def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# 验证单个平台报告的身份、轮次、相位、恢复率与零孤儿合同
def _validate_report(
    report: dict[str, Any],
    *,
    path: Path,
    expected_commit: str,
    iterations: int,
    min_rate: float,
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    platform = str(report.get("platform", ""))
    prefix = f"{path}:{platform or 'unknown'}"
    results = _object_list(report.get("results"))
    passed_results = sum(item.get("passed") is True for item in results)
    iteration_ids = [item.get("iteration") for item in results]
    llm_phases = sum(item.get("phase") == _PHASE_LLM for item in results)
    tool_phases = sum(item.get("phase") == _PHASE_TOOL for item in results)
    orphan_lists_valid = all(
        isinstance(item.get("orphaned_tool_call_ids"), list) for item in results
    )
    result_orphans = sum(
        len(item.get("orphaned_tool_call_ids", []))
        for item in results
        if isinstance(item.get("orphaned_tool_call_ids", []), list)
    )
    expected_llm = (iterations + 1) // 2
    expected_tool = iterations // 2
    recovery_rate = passed_results / iterations if iterations else 0.0
    checks = (
        (report.get("schema_version") == 3, "schema_version must be 3"),
        (report.get("commit") == expected_commit, "commit does not match workflow SHA"),
        (platform in _REQUIRED_PLATFORMS, "platform is not supported"),
        (bool(report.get("architecture")), "architecture is missing"),
        (bool(report.get("python_version")), "python_version is missing"),
        (report.get("iterations") == iterations, "iterations mismatch"),
        (
            report.get("completed_iterations") == iterations,
            "completed_iterations mismatch",
        ),
        (len(results) == iterations, "results length mismatch"),
        (
            iteration_ids == list(range(1, iterations + 1)),
            "result iteration sequence mismatch",
        ),
        (
            all(isinstance(item.get("passed"), bool) for item in results),
            "every result must contain a boolean passed value",
        ),
        (report.get("passed") == passed_results, "passed count mismatch"),
        (
            math.isclose(
                float(report.get("recovery_rate", -1.0)),
                recovery_rate,
                abs_tol=1e-12,
            ),
            "recovery_rate does not match results",
        ),
        (recovery_rate >= min_rate, "recovery_rate is below aggregate threshold"),
        (
            math.isclose(
                float(report.get("min_rate", -1.0)),
                min_rate,
                abs_tol=1e-12,
            ),
            "min_rate does not match aggregate contract",
        ),
        (llm_phases == expected_llm, "LLM interruption phase count mismatch"),
        (tool_phases == expected_tool, "tool interruption phase count mismatch"),
        (orphan_lists_valid, "every result must contain an orphan id list"),
        (
            report.get("orphaned_tool_calls") == result_orphans == 0,
            "orphaned tool calls must be zero",
        ),
        (report.get("infrastructure_error") is None, "infrastructure_error is set"),
        (report.get("failure_context") is None, "failure_context is set"),
        (report.get("gate_passed") is True, "platform gate_passed is not true"),
    )
    for passed, message in checks:
        if not passed:
            errors.append(f"{prefix}: {message}")
    summary = {
        "path": path.as_posix(),
        "platform": platform,
        "commit": report.get("commit"),
        "iterations": report.get("iterations"),
        "completed_iterations": report.get("completed_iterations"),
        "passed": passed_results,
        "recovery_rate": recovery_rate,
        "llm_phases": llm_phases,
        "tool_phases": tool_phases,
        "orphaned_tool_calls": result_orphans,
        "valid": not errors,
    }
    return summary, errors


# 汇总目录内三份平台报告并生成可归档的跨平台门禁结论
def validate_reports(
    input_root: Path,
    *,
    expected_commit: str,
    iterations: int = 100,
    min_rate: float = 0.95,
) -> dict[str, Any]:
    errors: list[str] = []
    summaries: list[dict[str, Any]] = []
    paths = sorted(input_root.rglob("crash-recovery.json")) if input_root.exists() else []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: unreadable JSON: {type(exc).__name__}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"{path}: report root must be an object")
            continue
        summary, report_errors = _validate_report(
            payload,
            path=path,
            expected_commit=expected_commit,
            iterations=iterations,
            min_rate=min_rate,
        )
        summaries.append(summary)
        errors.extend(report_errors)
    platforms = [str(item["platform"]) for item in summaries]
    if len(paths) != len(_REQUIRED_PLATFORMS):
        errors.append(
            f"expected {len(_REQUIRED_PLATFORMS)} reports, found {len(paths)}"
        )
    if set(platforms) != _REQUIRED_PLATFORMS or len(platforms) != len(set(platforms)):
        errors.append(
            "platform set must be exactly " + ", ".join(sorted(_REQUIRED_PLATFORMS))
        )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": expected_commit,
        "iterations_per_platform": iterations,
        "min_rate": min_rate,
        "required_platforms": sorted(_REQUIRED_PLATFORMS),
        "reports": sorted(summaries, key=lambda item: str(item["platform"])),
        "totals": {
            "reports": len(summaries),
            "iterations": sum(int(item["iterations"] or 0) for item in summaries),
            "completed_iterations": sum(
                int(item["completed_iterations"] or 0) for item in summaries
            ),
            "passed": sum(int(item["passed"]) for item in summaries),
            "llm_phases": sum(int(item["llm_phases"]) for item in summaries),
            "tool_phases": sum(int(item["tool_phases"]) for item in summaries),
            "orphaned_tool_calls": sum(
                int(item["orphaned_tool_calls"]) for item in summaries
            ),
        },
        "errors": errors,
        "gate_passed": not errors,
    }


# 执行三平台聚合校验并始终先写报告再返回门禁退出码
def main() -> int:
    args = _parse_args()
    if args.iterations <= 0:
        raise SystemExit("--iterations must be positive")
    if not 0.0 <= args.min_rate <= 1.0:
        raise SystemExit("--min-rate must be between 0 and 1")
    report = validate_reports(
        args.input_root,
        expected_commit=args.commit,
        iterations=args.iterations,
        min_rate=args.min_rate,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    totals = report["totals"]
    print(
        "crash recovery aggregate: "
        f"{totals['passed']}/{totals['iterations']}; "
        f"orphans={totals['orphaned_tool_calls']}; "
        f"gate={report['gate_passed']}; output={args.output.resolve()}"
    )
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

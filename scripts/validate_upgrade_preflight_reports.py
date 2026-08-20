#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_EXPECTED_PLATFORMS = {"linux", "win32", "darwin"}


# 解析三平台报告、期望身份和聚合输出目录
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate and aggregate cross-platform upgrade preflight reports."
    )
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-baseline-ref", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


# 将 Git ref 解析为完整 commit 供聚合身份比较
def _resolve_commit(ref: str, *, root: Path = _ROOT) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    commit = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError(f"ref did not resolve to a full commit: {ref}")
    return commit


# 读取单份升级报告并要求顶层对象结构
def _load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid upgrade report {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"upgrade report is not an object: {path}")
    return payload


# 读取字典字段并在类型不符时返回空对象供统一问题收集
def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


# 校验三平台升级与回滚报告的一致身份和状态不变量
def validate_reports(
    paths: list[Path],
    *,
    expected_commit: str,
    expected_baseline_commit: str,
) -> dict[str, Any]:
    reports = [_load_report(path) for path in paths]
    issues: list[str] = []
    if len(reports) != 3:
        issues.append(f"expected 3 reports, found {len(reports)}")
    platforms = [str(report.get("platform", "")) for report in reports]
    if set(platforms) != _EXPECTED_PLATFORMS or len(set(platforms)) != len(platforms):
        issues.append(
            f"platforms must be exactly {sorted(_EXPECTED_PLATFORMS)}, got {platforms}"
        )

    baseline_versions: set[str] = set()
    candidate_versions: set[str] = set()
    summaries: list[dict[str, Any]] = []
    for index, report in enumerate(reports):
        label = platforms[index] or f"report-{index + 1}"
        baseline = _mapping(report, "baseline")
        candidate = _mapping(report, "candidate")
        phases = _mapping(report, "phases")
        baseline_phase = _mapping(phases, "baseline")
        upgrade_phase = _mapping(phases, "upgrade")
        rollback_phase = _mapping(phases, "rollback")
        if report.get("schema_version") != 1 or report.get("status") != "passed":
            issues.append(f"{label}: report status/schema is not passed v1")
        if baseline.get("commit") != expected_baseline_commit:
            issues.append(f"{label}: baseline commit mismatch")
        if candidate.get("commit") != expected_commit:
            issues.append(f"{label}: candidate commit mismatch")
        if candidate.get("dirty") is not False:
            issues.append(f"{label}: candidate worktree was dirty")
        baseline_version = str(baseline.get("version", ""))
        candidate_version = str(candidate.get("version", ""))
        baseline_versions.add(baseline_version)
        candidate_versions.add(candidate_version)
        baseline_thread = str(baseline_phase.get("thread_id", ""))
        candidate_thread = str(upgrade_phase.get("created_thread_id", ""))
        if not baseline_thread or upgrade_phase.get("retained_thread_id") != baseline_thread:
            issues.append(f"{label}: upgrade did not retain the baseline thread")
        if not candidate_thread or candidate_thread == baseline_thread:
            issues.append(f"{label}: upgrade candidate thread identity is invalid")
        if rollback_phase.get("restored_thread_id") != baseline_thread:
            issues.append(f"{label}: rollback did not restore the baseline thread")
        if rollback_phase.get("version") != baseline_version:
            issues.append(f"{label}: rollback package version mismatch")
        if rollback_phase.get("backup_hash_matches") is not True:
            issues.append(f"{label}: rollback backup digest mismatch")
        baseline_count = baseline_phase.get("thread_count")
        upgrade_count = upgrade_phase.get("thread_count")
        rollback_count = rollback_phase.get("thread_count")
        if not isinstance(baseline_count, int) or baseline_count < 1:
            issues.append(f"{label}: invalid baseline thread count")
        elif upgrade_count != baseline_count + 1:
            issues.append(f"{label}: upgrade thread count must increase by one")
        if rollback_count != baseline_count:
            issues.append(f"{label}: rollback thread count did not return to baseline")
        summaries.append(
            {
                "platform": label,
                "baseline_version": baseline_version,
                "candidate_version": candidate_version,
                "baseline_thread_count": baseline_count,
                "upgrade_thread_count": upgrade_count,
                "rollback_thread_count": rollback_count,
                "backup_sha256": report.get("backup_sha256"),
            }
        )
    if len(baseline_versions) != 1:
        issues.append(f"baseline versions differ: {sorted(baseline_versions)}")
    if len(candidate_versions) != 1:
        issues.append(f"candidate versions differ: {sorted(candidate_versions)}")
    if issues:
        raise ValueError("upgrade preflight aggregate failed:\n- " + "\n- ".join(issues))
    return {
        "schema_version": 1,
        "status": "passed",
        "candidate_commit": expected_commit,
        "baseline_commit": expected_baseline_commit,
        "platforms": sorted(platforms),
        "baseline_version": next(iter(baseline_versions)),
        "candidate_version": next(iter(candidate_versions)),
        "reports": sorted(summaries, key=lambda item: str(item["platform"])),
    }


# 写入稳定 JSON 与 Markdown 聚合报告
def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "upgrade-preflight-aggregate.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    lines = [
        "# CodeRook upgrade preflight aggregate",
        "",
        f"- Status: **{report['status']}**",
        f"- Candidate commit: `{report['candidate_commit']}`",
        f"- Baseline commit: `{report['baseline_commit']}`",
        f"- Versions: `{report['baseline_version']}` -> `{report['candidate_version']}` -> `{report['baseline_version']}`",
        f"- Platforms: {', '.join(report['platforms'])}",
        "",
    ]
    (output_dir / "upgrade-preflight-aggregate.md").write_text(
        "\n".join(lines),
        encoding="utf-8",
        newline="\n",
    )


# 聚合三平台报告并将失败转换为明确退出信息
def main() -> int:
    args = _parse_args()
    try:
        baseline = _resolve_commit(args.expected_baseline_ref)
        report = validate_reports(
            args.reports,
            expected_commit=args.expected_commit,
            expected_baseline_commit=baseline,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    _write_report(report, args.output_dir)
    print(
        "upgrade preflight aggregate passed: "
        f"{report['baseline_version']} -> {report['candidate_version']} "
        f"platforms={','.join(report['platforms'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

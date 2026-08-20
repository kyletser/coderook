from __future__ import annotations

import hashlib
import json
import re

from code_rook.benchmark.models import (
    BenchmarkReport,
    BenchmarkRunConfig,
    BenchmarkTaskContract,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


# 对 JSON 可序列化材料生成排序稳定的 SHA-256
def canonical_fingerprint(material: object) -> str:
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# 从 commit、suite、完整 run config 与任务合同重算候选身份
def candidate_fingerprint(
    run_config: BenchmarkRunConfig,
    contracts: list[BenchmarkTaskContract],
    *,
    repository_commit: str,
    suite: str | None,
) -> str:
    config_material = run_config.model_dump(mode="json")
    config_material.pop("candidate_fingerprint", None)
    return canonical_fingerprint(
        {
            "repository_commit": repository_commit,
            "suite": suite,
            "run_config": config_material,
            "task_contracts": [
                contract.model_dump(mode="json") for contract in contracts
            ],
        }
    )


# 返回真实候选报告缺失的 route、commit、任务、预算或指纹合同项
def find_candidate_contract_issues(report: BenchmarkReport) -> list[str]:
    config = report.run_config
    issues: list[str] = []
    if _GIT_SHA_RE.fullmatch(report.repository_commit) is None:
        issues.append("repository_commit must be a full 40-character Git SHA")
    for field_name in ("route_id", "model", "wire_format", "config_fingerprint"):
        value = getattr(config, field_name)
        if not value or value == "unknown":
            issues.append(f"run_config.{field_name} must be explicit")
    for field_name in (
        "config_fingerprint",
        "task_catalog_fingerprint",
        "fixture_fingerprint",
        "budget_fingerprint",
        "candidate_fingerprint",
    ):
        value = getattr(config, field_name)
        if _SHA256_RE.fullmatch(value) is None:
            issues.append(f"run_config.{field_name} must be a SHA-256")
    result_ids = [result.task_id for result in report.results]
    contract_ids = [contract.task_id for contract in report.task_contracts]
    if len(result_ids) != len(set(result_ids)):
        issues.append("report results contain duplicate task ids")
    if sorted(result_ids) != sorted(contract_ids):
        issues.append("task contracts do not match report results")
    if config.task_count != len(report.task_contracts):
        issues.append("run_config.task_count does not match task contracts")
    expected_candidate = candidate_fingerprint(
        config,
        report.task_contracts,
        repository_commit=report.repository_commit,
        suite=report.suite,
    )
    if config.candidate_fingerprint != expected_candidate:
        issues.append("run_config.candidate_fingerprint does not match report material")
    if config.router == "static":
        for result in report.results:
            execution = result.execution
            for field_name in ("route_id", "model", "wire_format"):
                actual = getattr(execution, field_name)
                expected = getattr(config, field_name)
                if actual and actual != expected:
                    issues.append(
                        f"task {result.task_id} {field_name} {actual!r} != {expected!r}"
                    )
    return issues


# 在写报告前硬失败，禁止 unknown 或可被篡改的候选合同落盘
def require_candidate_contract(report: BenchmarkReport) -> None:
    issues = find_candidate_contract_issues(report)
    if issues:
        raise ValueError("benchmark candidate contract failed:\n- " + "\n- ".join(issues))

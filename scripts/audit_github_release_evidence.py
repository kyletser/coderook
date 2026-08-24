#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

JsonValue = dict[str, Any] | list[Any]
FetchResult = tuple[JsonValue | None, str | None]
Fetcher = Callable[[str, str], FetchResult]

_API = "https://api.github.com"
_REQUIRED_CHECKS = {"Required Ubuntu gate"}
_WORKFLOWS: dict[str, int] = {
    "ci.yml": 3,
    "security.yml": 1,
    "distribution.yml": 1,
    "crash-recovery.yml": 1,
    "mcp-interop.yml": 1,
    "benchmark-release.yml": 1,
}


class WorkflowEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow: str
    required_consecutive_successes: int = Field(ge=1)
    consecutive_successes: int = Field(ge=0)
    head_matches_default: bool
    passed: bool
    latest_run_id: int | None = None
    latest_url: str | None = None
    latest_head_sha: str | None = None
    latest_status: str | None = None
    latest_conclusion: str | None = None
    latest_created_at: str | None = None
    error: str | None = None


class RulesetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "unknown"]
    active_ruleset_ids: list[int] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    rule_types: list[str] = Field(default_factory=list)
    error: str | None = None


class GitHubReleaseEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: str
    repository: str
    default_branch: str
    default_branch_sha: str
    workflows: list[WorkflowEvidence]
    ruleset: RulesetEvidence
    gate_passed: bool
    gate_reasons: list[str]


# 解析远端证据审计器参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit GitHub workflow and branch-ruleset release evidence."
    )
    parser.add_argument("--repo", required=True, help="owner/repository")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/github-release-evidence.json"),
    )
    parser.add_argument("--report-only", action="store_true")
    return parser.parse_args()


# 调用 GitHub REST API 并把权限或网络错误转换为可归档状态
def _fetch_json(path: str, token: str) -> FetchResult:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "coderook-release-evidence-auditor",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(f"{_API}{path}", headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            return cast(JsonValue, json.load(response)), None
    except HTTPError as exc:
        return None, f"http_{exc.code}"
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"network_or_decode_error:{type(exc).__name__}"


# 统计从最新完成 run 开始的连续成功次数并保留最近运行身份
def _workflow_evidence(
    workflow: str,
    required: int,
    default_sha: str,
    payload: JsonValue | None,
    error: str | None,
) -> WorkflowEvidence:
    if error is not None or not isinstance(payload, dict):
        return WorkflowEvidence(
            workflow=workflow,
            required_consecutive_successes=required,
            consecutive_successes=0,
            head_matches_default=False,
            passed=False,
            error=error or "invalid_payload",
        )
    raw_runs = payload.get("workflow_runs", [])
    runs = [run for run in raw_runs if isinstance(run, dict)]
    completed = [run for run in runs if run.get("status") == "completed"]
    latest = completed[0] if completed else (runs[0] if runs else {})
    consecutive = 0
    for run in completed:
        if run.get("conclusion") != "success":
            break
        consecutive += 1
    latest_head_sha = str(latest["head_sha"]) if latest.get("head_sha") else None
    head_matches_default = default_sha != "unknown" and latest_head_sha == default_sha
    return WorkflowEvidence(
        workflow=workflow,
        required_consecutive_successes=required,
        consecutive_successes=consecutive,
        head_matches_default=head_matches_default,
        passed=consecutive >= required and head_matches_default,
        latest_run_id=(
            int(latest["id"]) if isinstance(latest.get("id"), int) else None
        ),
        latest_url=(
            str(latest["html_url"]) if latest.get("html_url") else None
        ),
        latest_head_sha=latest_head_sha,
        latest_status=(str(latest["status"]) if latest.get("status") else None),
        latest_conclusion=(
            str(latest["conclusion"]) if latest.get("conclusion") else None
        ),
        latest_created_at=(
            str(latest["created_at"]) if latest.get("created_at") else None
        ),
    )


# 检查 active branch ruleset 是否同时锁定 PR、删除、强推与稳定必需检查
def _ruleset_evidence(
    payload: JsonValue | None,
    error: str | None,
    default_branch: str,
) -> RulesetEvidence:
    if error is not None:
        return RulesetEvidence(status="unknown", error=error)
    if not isinstance(payload, list):
        return RulesetEvidence(status="unknown", error="invalid_payload")
    active = [
        item
        for item in payload
        if isinstance(item, dict)
        and item.get("target") == "branch"
        and item.get("enforcement") == "active"
    ]
    applicable = []
    for item in active:
        conditions = item.get("conditions", {})
        ref_name = conditions.get("ref_name", {}) if isinstance(conditions, dict) else {}
        include = ref_name.get("include", []) if isinstance(ref_name, dict) else []
        exclude = ref_name.get("exclude", []) if isinstance(ref_name, dict) else []
        default_refs = {"~DEFAULT_BRANCH", default_branch, f"refs/heads/{default_branch}"}
        included = not include or bool(default_refs.intersection(map(str, include)))
        excluded = bool(default_refs.intersection(map(str, exclude)))
        if included and not excluded:
            applicable.append(item)
    ruleset_ids = sorted(
        int(item["id"])
        for item in applicable
        if isinstance(item.get("id"), int)
    )
    rule_types: set[str] = set()
    checks: set[str] = set()
    for item in applicable:
        rules = item.get("rules", [])
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_type = str(rule.get("type", ""))
            if rule_type:
                rule_types.add(rule_type)
            parameters = rule.get("parameters", {})
            if rule_type != "required_status_checks" or not isinstance(
                parameters, dict
            ):
                continue
            contexts = parameters.get("required_status_checks", [])
            if isinstance(contexts, list):
                for context in contexts:
                    if isinstance(context, dict) and context.get("context"):
                        checks.add(str(context["context"]))
    required_types = {"pull_request", "deletion", "non_fast_forward"}
    passed = (
        bool(applicable)
        and required_types <= rule_types
        and _REQUIRED_CHECKS <= checks
    )
    return RulesetEvidence(
        status="passed" if passed else "failed",
        active_ruleset_ids=ruleset_ids,
        required_checks=sorted(checks),
        rule_types=sorted(rule_types),
    )


# 获取 ruleset 详情，因为列表端点不保证内嵌完整 rules 数组
def _ruleset_details(
    repository: str,
    payload: JsonValue | None,
    token: str,
    fetcher: Fetcher,
) -> FetchResult:
    if not isinstance(payload, list):
        return payload, None
    details: list[Any] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            continue
        detail, error = fetcher(
            f"/repos/{repository}/rulesets/{item['id']}",
            token,
        )
        if error is not None:
            return None, f"ruleset_detail_{error}"
        if isinstance(detail, dict):
            details.append(detail)
    return details, None


# 从 GitHub API 汇总默认分支、六类 workflow 与 ruleset 的外部门禁证据
def collect_github_release_evidence(
    repository: str,
    branch: str,
    token: str,
    *,
    fetcher: Fetcher = _fetch_json,
) -> GitHubReleaseEvidence:
    repo_payload, repo_error = fetcher(f"/repos/{repository}", token)
    if repo_error is not None or not isinstance(repo_payload, dict):
        raise ValueError(f"repository metadata unavailable: {repo_error}")
    default_branch = str(repo_payload.get("default_branch", branch))
    branch_payload, branch_error = fetcher(
        f"/repos/{repository}/branches/{default_branch}", token
    )
    default_sha = "unknown"
    if branch_error is None and isinstance(branch_payload, dict):
        commit = branch_payload.get("commit", {})
        if isinstance(commit, dict) and commit.get("sha"):
            default_sha = str(commit["sha"])

    workflows = []
    for workflow, required in _WORKFLOWS.items():
        payload, error = fetcher(
            f"/repos/{repository}/actions/workflows/{workflow}/runs"
            f"?branch={default_branch}&per_page=20",
            token,
        )
        workflows.append(
            _workflow_evidence(
                workflow,
                required,
                default_sha,
                payload,
                error,
            )
        )
    rulesets, ruleset_error = fetcher(
        f"/repos/{repository}/rulesets?includes_parents=false",
        token,
    )
    if ruleset_error is None:
        rulesets, ruleset_error = _ruleset_details(
            repository,
            rulesets,
            token,
            fetcher,
        )
    ruleset = _ruleset_evidence(rulesets, ruleset_error, default_branch)
    reasons = []
    for evidence in workflows:
        if evidence.passed:
            continue
        reason = (
            "workflow_stale"
            if evidence.consecutive_successes
            >= evidence.required_consecutive_successes
            and not evidence.head_matches_default
            else "workflow_not_ready"
        )
        reasons.append(f"{reason}:{evidence.workflow}")
    if default_sha == "unknown":
        reasons.append("default_branch_sha_unknown")
    if ruleset.status != "passed":
        reasons.append(f"ruleset_{ruleset.status}")
    return GitHubReleaseEvidence(
        generated_at=datetime.now(UTC).isoformat(),
        repository=repository,
        default_branch=default_branch,
        default_branch_sha=default_sha,
        workflows=workflows,
        ruleset=ruleset,
        gate_passed=not reasons,
        gate_reasons=reasons,
    )


# 执行远端审计并始终先写报告，再按门禁决定退出码
def main() -> int:
    args = _parse_args()
    token = os.environ.get(args.token_env, "")
    try:
        report = collect_github_release_evidence(
            args.repo,
            args.branch,
            token,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        report.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"GitHub release evidence: {'PASS' if report.gate_passed else 'FAIL'}; "
        f"sha={report.default_branch_sha}; report={args.output.resolve()}"
    )
    return 0 if args.report_only or report.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

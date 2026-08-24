from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.audit_github_release_evidence import (
    FetchResult,
    collect_github_release_evidence,
)


# 构造包含指定结论的 GitHub workflow runs 响应
def _runs(*conclusions: str) -> dict[str, Any]:
    return {
        "workflow_runs": [
            {
                "id": index,
                "html_url": f"https://example.test/runs/{index}",
                "head_sha": "a" * 40,
                "status": "completed",
                "conclusion": conclusion,
                "created_at": f"2026-08-{20 - index:02d}T00:00:00Z",
            }
            for index, conclusion in enumerate(conclusions, 1)
        ]
    }


# 功能：验证连续三次 CI、其余 workflow 与 active ruleset 齐备时远端门禁通过
# 设计：用内存 fetcher 覆盖全部 GitHub REST 路径，避免单测依赖网络、账号权限或实时 Actions 状态
def test_remote_release_evidence_requires_complete_external_proof() -> None:
    rulesets = [
        {
            "id": 7,
            "target": "branch",
            "enforcement": "active",
            "conditions": {
                "ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}
            },
            "rules": [
                {"type": "pull_request"},
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "Required Ubuntu gate"},
                        ]
                    },
                },
            ],
        }
    ]

    # 为远端审计提供完全可控的成功响应
    def fetcher(path: str, _token: str) -> FetchResult:
        if path == "/repos/kyletser/coderook":
            return {"default_branch": "main"}, None
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "a" * 40}}, None
        if "/rulesets?" in path:
            return [{"id": 7, "target": "branch", "enforcement": "active"}], None
        if path.endswith("/rulesets/7"):
            return rulesets[0], None
        if "/ci.yml/" in path:
            return _runs("success", "success", "success"), None
        if "/actions/workflows/" in path:
            return _runs("success"), None
        raise AssertionError(path)

    report = collect_github_release_evidence(
        "kyletser/coderook",
        "main",
        "test-token",
        fetcher=fetcher,
    )

    assert report.gate_passed is True
    assert report.default_branch_sha == "a" * 40
    assert report.ruleset.status == "passed"
    ci = next(item for item in report.workflows if item.workflow == "ci.yml")
    assert ci.consecutive_successes == 3
    assert ci.head_matches_default is True


# 功能：验证最新 CI 失败、缺失 workflow 与不可读 ruleset 都会保留独立失败原因
# 设计：混合失败、404 和空响应，断言审计器不把 API 不可见误判为门禁通过
def test_remote_release_evidence_fails_closed_on_missing_proof() -> None:
    # 模拟当前公开仓库中失败或尚未运行的外部证据
    def fetcher(path: str, _token: str) -> FetchResult:
        if path == "/repos/kyletser/coderook":
            return {"default_branch": "main"}, None
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "b" * 40}}, None
        if "/rulesets?" in path:
            return None, "http_403"
        if "/ci.yml/" in path:
            return _runs("failure", "success"), None
        return {"workflow_runs": []}, None

    report = collect_github_release_evidence(
        "kyletser/coderook",
        "main",
        "",
        fetcher=fetcher,
    )

    assert report.gate_passed is False
    assert "workflow_not_ready:ci.yml" in report.gate_reasons
    assert "workflow_not_ready:distribution.yml" in report.gate_reasons
    assert "ruleset_unknown" in report.gate_reasons
    assert report.ruleset.error == "http_403"


# 功能：验证旧 commit 成功记录与不适用于 main 的 ruleset 不能证明当前候选就绪
# 设计：让三次 CI 全绿但 head SHA 落后，并让 active ruleset 只匹配 dev，断言分别报告 stale 与 failed
def test_remote_release_evidence_rejects_stale_runs_and_other_branch_rules() -> None:
    ruleset = {
        "id": 8,
        "target": "branch",
        "enforcement": "active",
        "conditions": {
            "ref_name": {"include": ["refs/heads/dev"], "exclude": []}
        },
        "rules": [],
    }

    # 返回旧 commit 的成功 run 和只保护 dev 的规则集
    def fetcher(path: str, _token: str) -> FetchResult:
        if path == "/repos/kyletser/coderook":
            return {"default_branch": "main"}, None
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "b" * 40}}, None
        if "/rulesets?" in path:
            return [{"id": 8}], None
        if path.endswith("/rulesets/8"):
            return ruleset, None
        return _runs("success", "success", "success"), None

    report = collect_github_release_evidence(
        "kyletser/coderook",
        "main",
        "",
        fetcher=fetcher,
    )

    assert "workflow_stale:ci.yml" in report.gate_reasons
    assert report.workflows[0].head_matches_default is False
    assert report.ruleset.status == "failed"


# 功能：验证远端证据 workflow 和通用 Actions 不再使用已触发 Node 20 警告的旧代际
# 设计：扫描全部 workflow 的精确旧引用，同时锁定手动审计入口和 artifact 名，防止部分文件遗漏升级
def test_workflows_use_node24_generation_actions_and_remote_audit() -> None:
    root = Path(__file__).resolve().parents[2]
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / ".github" / "workflows").glob("*.yml"))
    )

    for stale in (
        "actions/checkout@v4",
        "actions/setup-node@v4",
        "actions/upload-artifact@v4",
        "actions/download-artifact@v4",
        "astral-sh/setup-uv@v6",
    ):
        assert stale not in workflows
    assert "audit_github_release_evidence.py" in workflows
    assert "github-release-evidence" in workflows

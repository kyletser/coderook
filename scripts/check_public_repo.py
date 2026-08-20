#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import unquote

_ROOT = Path(__file__).resolve().parent.parent
_REQUIRED_FILES = (
    "LICENSE",
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "SUPPORT.md",
    "GOVERNANCE.md",
    "ROADMAP.md",
    "CHANGELOG.md",
    "docs/README.md",
    "docs/status/OPEN_SOURCE_COMPLETION_PLAN.md",
    "docs/guides/UPGRADING.md",
    "docs/reference/THREAT_MODEL.md",
    "docs/reference/PUBLIC_BENCHMARKS.md",
    "docs/reference/COMPATIBILITY.md",
    "docs/operations/RELEASING.md",
    "docs/operations/BRANCH_PROTECTION.md",
    "docs/operations/MAINTAINERS.md",
    "docs/operations/CONTRIBUTOR_TASKS.md",
    "docs/career/PROJECT_CASE_STUDY.md",
    "docs/career/RESUME_EVIDENCE.md",
    "docs/career/INTERVIEW_GUIDE.md",
    "docs/reference/MCP_COMPATIBILITY.md",
    "docs/evidence/mcp-official-sdk-2.0.0/mcp-official-interop.json",
    "docs/evidence/mcp-official-sdk-2.0.0/mcp-official-interop.md",
    "docs/postmortems/README.md",
    "docs/postmortems/2026-08-19-cross-platform-ci.md",
    "docs/postmortems/2026-08-17-tui-refactor.md",
    "docs/images/coderook-tui.svg",
    "benchmarks/public/Dockerfile",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/contributor_task.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/CODEOWNERS",
    ".github/dependabot.yml",
    "examples/README.md",
    "examples/read_only_review.py",
    "examples/automated_fix.py",
    "examples/mcp_echo_server.py",
    "examples/skills/focused-fix/SKILL.md",
    "examples/hooks/guard_sensitive_files.py",
    "examples/hooks/hooks.toml",
    "scripts/capture_tui_demo.py",
    "tests/unit/test_tui_demo.py",
    "scripts/check_release_contract.py",
    "scripts/generate_release_manifest.py",
    "scripts/run_mcp_official_interop.py",
    "scripts/aggregate_benchmark_reports.py",
    "scripts/benchmark_optimization.py",
    "scripts/smoke_installed_runtime.py",
    "scripts/run_vscode_extension_host_smoke.py",
    "scripts/audit_github_release_evidence.py",
    "scripts/run_process_supervisor_benchmark.py",
    "scripts/validate_crash_recovery_reports.py",
    "tests/fixtures/mcp_official_server.py",
    "tests/unit/test_mcp_interop_report.py",
    "tests/unit/test_benchmark_aggregate.py",
    "tests/unit/test_distribution_smoke.py",
    "tests/unit/test_vscode_extension_host.py",
    "tests/unit/test_remote_release_evidence.py",
    "tests/unit/test_process_supervisor_benchmark.py",
    "tests/unit/test_crash_recovery_aggregate.py",
    ".github/workflows/mcp-interop.yml",
    "tests/unit/test_release_contract.py",
    ".github/workflows/release.yml",
    ".github/workflows/distribution.yml",
    ".github/workflows/remote-evidence.yml",
    ".github/workflows/crash-recovery.yml",
)
_REQUIRED_PROJECT_FIELDS = (
    "description",
    "readme",
    "license",
    "authors",
    "keywords",
    "classifiers",
)
_REQUIRED_PROJECT_URLS = ("Homepage", "Documentation", "Repository", "Issues", "Changelog")
_MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_DOCS_ROOT_FILES = {"README.md"}
_DOCS_ROOT_DIRECTORIES = {
    "archive",
    "career",
    "evidence",
    "guides",
    "images",
    "operations",
    "plans",
    "postmortems",
    "reference",
    "status",
}
_README_REQUIRED_LINKS = (
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "CHANGELOG.md",
    "LICENSE",
    "docs/reference/COMPATIBILITY.md",
    "ROADMAP.md",
    "docs/operations/CONTRIBUTOR_TASKS.md",
    "docs/operations/MAINTAINERS.md",
    "docs/career/PROJECT_CASE_STUDY.md",
)
_REQUIRED_WORKFLOW_SNIPPETS = {
    ".github/workflows/ci.yml": (
        "branches: [main]",
        "name: Required CI gate",
        "needs: [quality-and-package]",
        "if: always()",
        "ProcessSupervisor resource baseline",
        "process-supervisor-${{ runner.os }}",
    ),
    ".github/workflows/security.yml": (
        "github.actor != 'dependabot[bot]'",
        "vars.DEPENDENCY_REVIEW_ENABLED == 'true'",
        "name: Required security gate",
        "needs: [secret-scan, dependency-review, codeql]",
        "if: always()",
    ),
    ".github/dependabot.yml": (
        "package-ecosystem: uv",
        "package-ecosystem: npm",
        "package-ecosystem: github-actions",
        "interval: monthly",
        "open-pull-requests-limit: 1",
        "patterns: [\"*\"]",
    ),
    ".github/workflows/distribution.yml": (
        "Run the full distribution gate or one focused job",
        "inputs.target == 'vscode'",
        "Container zero-credential Core, ping and TUI smoke",
        "Portable zero-credential Core, ping and TUI smoke",
        "smoke_installed_runtime.py",
        "Real daemon Extension Host smoke",
        "artifacts/vscode-extension-host.json",
        "artifacts/vscode-approval.png",
        "imagemagick xdotool xvfb",
    ),
    ".github/workflows/remote-evidence.yml": (
        "Audit remote release evidence",
        "audit_github_release_evidence.py",
        "github-release-evidence",
    ),
    ".github/workflows/crash-recovery.yml": (
        "name: Required crash recovery gate",
        "needs: [daemon-crash-matrix]",
        "validate_crash_recovery_reports.py",
        "crash-recovery-aggregate",
    ),
    "docs/operations/BRANCH_PROTECTION.md": (
        "`Required CI gate`",
        "`Required security gate`",
        "OS6-05 只能标记 `PARTIAL`",
    ),
    ".github/CODEOWNERS": ("* @kyletser",),
}
_REQUIRED_RESUME_SNIPPETS = {
    "docs/career/PROJECT_CASE_STUDY.md": (
        "评分卡为 **NO-GO**",
        "我独立设计并实现",
        "第三方",
    ),
    "docs/career/RESUME_EVIDENCE.md": (
        "## 当前禁止宣称",
        "## 可引用的历史精确数字",
        "日期/基线",
        "production-ready",
    ),
    "docs/career/INTERVIEW_GUIDE.md": (
        "3 分钟版本",
        "10 分钟版本",
        "当前评分卡仍 NO-GO",
    ),
    "docs/postmortems/2026-08-19-cross-platform-ci.md": (
        "CI #34/#35 远端三平台连续两次通过",
    ),
    "docs/postmortems/2026-08-17-tui-refactor.md": (
        "未达到的目标与停止理由",
        "不能从行数和测试数推导",
    ),
}
_TRACKED_POLLUTION_PATTERNS = (
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"\.py[co]$"),
    re.compile(r"^(build|dist|reports)/"),
    re.compile(r"^\.coderook/"),
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.git-credentials$"),
    re.compile(r"(^|/)(credentials\.json|ipc-token|api-token|runtime\.db)$"),
)


# 枚举公开仓库中需要校验链接的 Markdown 文件
def _markdown_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if path.is_file() and not any(part in _SKIP_PARTS for part in path.relative_to(root).parts)
    )


# 从 Markdown 目标中提取不含标题和锚点的本地路径
def _local_link_path(target: str) -> str | None:
    value = target.strip()
    if value.startswith("<") and ">" in value:
        value = value[1 : value.index(">")]
    else:
        value = value.split(maxsplit=1)[0]
    lowered = value.lower()
    if not value or value.startswith("#") or lowered.startswith(("http://", "https://", "mailto:", "data:")):
        return None
    return unquote(value.split("#", maxsplit=1)[0]) or None


# 查找缺失的开源治理与社区文件
def find_missing_required_files(root: Path = _ROOT) -> list[str]:
    return [relative for relative in _REQUIRED_FILES if not (root / relative).is_file()]


# 查找仓库 Markdown 中指向不存在本地目标的链接
def find_broken_markdown_links(root: Path = _ROOT) -> list[str]:
    findings: list[str] = []
    for path in _markdown_files(root):
        text = path.read_text(encoding="utf-8")
        for match in _MARKDOWN_LINK.finditer(text):
            local_target = _local_link_path(match.group(1))
            if local_target is None:
                continue
            resolved = path.parent / local_target
            if not resolved.exists():
                relative = path.relative_to(root).as_posix()
                findings.append(f"{relative} -> {match.group(1).strip()}")
    return findings


# 校验 docs 顶层只保留索引并按稳定用途目录分组
def find_docs_layout_issues(root: Path = _ROOT) -> list[str]:
    docs = root / "docs"
    if not docs.is_dir():
        return ["docs directory is missing"]
    files = {path.name for path in docs.iterdir() if path.is_file()}
    directories = {path.name for path in docs.iterdir() if path.is_dir()}
    issues = [
        f"unexpected docs root file: {name}"
        for name in sorted(files - _DOCS_ROOT_FILES)
    ]
    issues.extend(
        f"missing docs category: {name}"
        for name in sorted(_DOCS_ROOT_DIRECTORIES - directories)
    )
    issues.extend(
        f"unexpected docs category: {name}"
        for name in sorted(directories - _DOCS_ROOT_DIRECTORIES)
    )
    return issues


# 校验构建元数据是否包含公开发行所需字段与链接
def find_project_metadata_issues(root: Path = _ROOT) -> list[str]:
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        return ["pyproject.toml is missing"]
    with pyproject_path.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    issues = [f"project.{field} is missing" for field in _REQUIRED_PROJECT_FIELDS if not project.get(field)]
    urls = project.get("urls", {})
    issues.extend(f"project.urls.{name} is missing" for name in _REQUIRED_PROJECT_URLS if not urls.get(name))
    return issues


# 校验 README 是否暴露贡献、安全、支持、变更和许可证入口
def find_readme_contract_issues(root: Path = _ROOT) -> list[str]:
    readme = (root / "README.md").read_text(encoding="utf-8")
    issues = [f"README.md does not link {target}" for target in _README_REQUIRED_LINKS if f"]({target})" not in readme]
    stale_claim = "首次没有可用 LLM 配置时，会先进入 API 配置向导"
    if stale_claim in readme:
        issues.append("README.md still claims first-run API configuration is mandatory")
    if "50 任务离线 benchmark" not in readme:
        issues.append("README.md does not use the current 50-task benchmark claim")
    return issues


# 校验稳定必需检查、依赖更新、CODEOWNERS 与外部 ruleset 边界没有漂移
def find_governance_contract_issues(root: Path = _ROOT) -> list[str]:
    issues: list[str] = []
    for relative, snippets in _REQUIRED_WORKFLOW_SNIPPETS.items():
        path = root / relative
        if not path.is_file():
            issues.append(f"governance contract file is missing: {relative}")
            continue
        content = path.read_text(encoding="utf-8")
        issues.extend(
            f"{relative} does not contain required contract: {snippet}"
            for snippet in snippets
            if snippet not in content
        )
    return issues


# 校验简历材料始终保留职责归因、NO-GO 和历史数字限定词
def find_resume_evidence_contract_issues(root: Path = _ROOT) -> list[str]:
    issues: list[str] = []
    for relative, snippets in _REQUIRED_RESUME_SNIPPETS.items():
        path = root / relative
        if not path.is_file():
            issues.append(f"resume evidence file is missing: {relative}")
            continue
        content = path.read_text(encoding="utf-8")
        issues.extend(
            f"{relative} does not contain required evidence boundary: {snippet}"
            for snippet in snippets
            if snippet not in content
        )
    return issues


# 校验官方 MCP 报告绑定固定 SDK/commit 且三种 transport 的逐项结果全通过
def find_mcp_evidence_contract_issues(root: Path = _ROOT) -> list[str]:
    relative = "docs/evidence/mcp-official-sdk-2.0.0/mcp-official-interop.json"
    path = root / relative
    if not path.is_file():
        return [f"MCP evidence file is missing: {relative}"]
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"MCP evidence is unreadable: {type(exc).__name__}"]
    issues: list[str] = []
    if report.get("official_sdk") != "mcp[cli]==2.0.0":
        issues.append("MCP evidence does not pin official SDK 2.0.0")
    commit = report.get("commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        issues.append("MCP evidence does not bind a full Git commit")
    results = {
        result.get("transport"): result
        for result in report.get("results", [])
        if isinstance(result, dict)
    }
    required_capabilities = ("tools", "resources", "prompts", "cancellation", "reconnect")
    for transport in ("stdio", "sse", "streamable-http"):
        result = results.get(transport, {})
        if result.get("status") != "passed":
            issues.append(f"MCP evidence transport did not pass: {transport}")
        issues.extend(
            f"MCP evidence {transport} did not pass {capability}"
            for capability in required_capabilities
            if result.get(capability) is not True
        )
    return issues


# 查找被 Git 跟踪的缓存、凭据和本地产物路径
def find_tracked_pollution(root: Path = _ROOT) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    tracked = result.stdout.splitlines()
    return [
        path
        for path in tracked
        if (root / path).exists()
        and any(pattern.search(path.replace("\\", "/")) for pattern in _TRACKED_POLLUTION_PATTERNS)
    ]


# 汇总公开仓库契约的全部失败项
def collect_public_repo_issues(root: Path = _ROOT) -> list[str]:
    issues: list[str] = []
    issues.extend(f"missing required file: {path}" for path in find_missing_required_files(root))
    issues.extend(f"broken markdown link: {link}" for link in find_broken_markdown_links(root))
    issues.extend(find_docs_layout_issues(root))
    issues.extend(find_project_metadata_issues(root))
    issues.extend(find_readme_contract_issues(root))
    issues.extend(find_governance_contract_issues(root))
    issues.extend(find_resume_evidence_contract_issues(root))
    issues.extend(find_mcp_evidence_contract_issues(root))
    issues.extend(f"tracked local artifact: {path}" for path in find_tracked_pollution(root))
    return issues


# 作为 CI 门禁校验开源治理、文档、元数据和仓库卫生
def main() -> None:
    issues = collect_public_repo_issues()
    if issues:
        raise SystemExit("Public repository contract failed:\n" + "\n".join(f"- {item}" for item in issues))
    print("CodeRook public repository contract passed.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

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
    "CHANGELOG.md",
    "docs/README.md",
    "docs/OPEN_SOURCE_COMPLETION_PLAN.md",
    "docs/UPGRADING.md",
    "docs/THREAT_MODEL.md",
    "docs/PUBLIC_BENCHMARKS.md",
    "docs/COMPATIBILITY.md",
    "docs/images/coderook-tui.svg",
    "benchmarks/public/Dockerfile",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    "examples/README.md",
    "examples/read_only_review.py",
    "examples/automated_fix.py",
    "examples/mcp_echo_server.py",
    "scripts/capture_tui_demo.py",
    "tests/unit/test_tui_demo.py",
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
_README_REQUIRED_LINKS = (
    "CONTRIBUTING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "CHANGELOG.md",
    "LICENSE",
    "docs/COMPATIBILITY.md",
)
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
    issues.extend(find_project_metadata_issues(root))
    issues.extend(find_readme_contract_issues(root))
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

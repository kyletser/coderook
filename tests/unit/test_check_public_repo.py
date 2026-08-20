from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_public_repo import (
    find_broken_markdown_links,
    find_docs_layout_issues,
    find_governance_contract_issues,
    find_project_metadata_issues,
    find_resume_evidence_contract_issues,
    find_tracked_pollution,
)


# 功能：验证文档顶层只允许索引文件和约定用途目录
# 设计：先构造完整分类再加入散落 Markdown，覆盖规范结构与重新变乱两个分支
def test_find_docs_layout_issues_rejects_flat_markdown(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# index\n", encoding="utf-8")
    for name in (
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
    ):
        (docs / name).mkdir()

    assert find_docs_layout_issues(tmp_path) == []

    (docs / "LOOSE_NOTES.md").write_text("# loose\n", encoding="utf-8")
    assert find_docs_layout_issues(tmp_path) == [
        "unexpected docs root file: LOOSE_NOTES.md"
    ]


# 功能：验证 Markdown 本地链接检查能区分存在目标与断链目标
# 设计：在临时目录构造一个有效链接和一个缺失链接，避免依赖真实仓库内容
def test_find_broken_markdown_links_reports_only_missing_target(tmp_path: Path) -> None:
    (tmp_path / "target.md").write_text("# target\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[ok](target.md) [bad](missing.md) [web](https://example.com)\n",
        encoding="utf-8",
    )

    assert find_broken_markdown_links(tmp_path) == ["README.md -> missing.md"]


# 功能：验证发行元数据检查会报告缺失字段并接受完整最小配置
# 设计：先写不完整 TOML 再覆盖为完整 TOML，覆盖失败与成功两个确定性分支
def test_find_project_metadata_issues_requires_public_fields(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "demo"\n', encoding="utf-8")
    assert "project.description is missing" in find_project_metadata_issues(tmp_path)

    pyproject.write_text(
        """[project]
name = "demo"
description = "demo"
readme = "README.md"
license = "MIT"
authors = [{name = "contributors"}]
keywords = ["agent"]
classifiers = ["Development Status :: 3 - Alpha"]

[project.urls]
Homepage = "https://example.com"
Documentation = "https://example.com/docs"
Repository = "https://example.com/repo"
Issues = "https://example.com/issues"
Changelog = "https://example.com/changelog"
""",
        encoding="utf-8",
    )
    assert find_project_metadata_issues(tmp_path) == []


# 功能：验证仓库卫生检查能识别被 Git 跟踪的缓存和凭据文件
# 设计：初始化隔离临时仓库并仅暂存代表性路径，避免读取开发者真实 Git 状态
def test_find_tracked_pollution_detects_sensitive_artifacts(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "safe.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TOKEN=placeholder\n", encoding="utf-8")
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.pyc").write_bytes(b"placeholder")
    subprocess.run(["git", "add", "-f", "."], cwd=tmp_path, check=True)

    assert find_tracked_pollution(tmp_path) == [".env", "pkg/__pycache__/mod.pyc"]


# 功能：验证公开仓库合同能发现稳定必需检查或 ruleset 诚实边界被删除
# 设计：在隔离目录只写不完整 CI 文件，断言缺失文件和缺失汇总门禁都被报告
def test_find_governance_contract_issues_reports_drift(tmp_path: Path) -> None:
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text("name: Required CI gate\n", encoding="utf-8")

    issues = find_governance_contract_issues(tmp_path)

    assert any("needs: [quality-and-package]" in issue for issue in issues)
    assert any("security.yml" in issue and "missing" in issue for issue in issues)


# 功能：验证真实仓库的 CI、安全、依赖更新、CODEOWNERS 与保护文档使用同一合同
# 设计：直接检查受版本控制资产，防止任一 workflow 改名后单元夹具仍自洽却让 GitHub ruleset 失效
def test_repository_governance_contract_is_consistent() -> None:
    root = Path(__file__).resolve().parents[2]

    assert find_governance_contract_issues(root) == []


# 功能：验证简历证据合同会发现职责归因、NO-GO 或历史数字限定词被删
# 设计：用空临时目录触发缺失资产分支，避免复制一套可能与真实材料共同漂移的假内容
def test_find_resume_evidence_contract_issues_requires_evidence_files(
    tmp_path: Path,
) -> None:
    issues = find_resume_evidence_contract_issues(tmp_path)

    assert any("PROJECT_CASE_STUDY.md" in issue and "missing" in issue for issue in issues)
    assert any("RESUME_EVIDENCE.md" in issue and "missing" in issue for issue in issues)


# 功能：验证真实项目案例、讲解、指标账本与两份复盘保留诚实边界
# 设计：检查版本控制中的正式材料，让夸大表述或删除失败复盘在普通单测阶段直接失败
def test_repository_resume_evidence_contract_is_consistent() -> None:
    root = Path(__file__).resolve().parents[2]

    assert find_resume_evidence_contract_issues(root) == []

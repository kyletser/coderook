from __future__ import annotations

from pathlib import Path

import pytest
from scripts.smoke_installed_runtime import first_run_environment


# 功能：验证 installed-runtime smoke 不继承 API key、token、CodeRook 状态或 PYTHONPATH
# 设计：注入代表性秘密和本地配置后检查清洗结果，同时保留无关环境变量以避免构造不现实的空环境
def test_first_run_environment_scrubs_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("CUSTOM_TOKEN", "secret")
    monkeypatch.setenv("CODEROOK_PORT", "7437")
    monkeypatch.setenv("PYTHONPATH", "developer-site")
    monkeypatch.setenv("SAFE_SETTING", "kept")

    env = first_run_environment(tmp_path)

    assert "ANTHROPIC_API_KEY" not in env
    assert "CUSTOM_TOKEN" not in env
    assert env["CODEROOK_PORT"] != "7437"
    assert "PYTHONPATH" not in env
    assert env["SAFE_SETTING"] == "kept"
    assert env["HOME"] == str(tmp_path)


# 功能：验证 Docker 与 Windows portable 均运行真实 installed-runtime smoke
# 设计：检查可复用 distribution workflow 的两处调用，防止发行门禁退化回只打印版本
def test_distribution_workflow_smokes_container_and_portable_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "distribution.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("smoke_installed_runtime.py") == 2
    assert "Container zero-credential Core, ping and TUI smoke" in workflow
    assert "Portable zero-credential Core, ping and TUI smoke" in workflow

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.smoke_wheel import _first_run_environment


# 功能：验证首次运行环境不会继承开发机模型密钥、Token 和旧 CodeRook 配置
# 设计：注入多种常见凭据后缀与无关变量，断言只清理敏感项并覆盖隔离 HOME
def test_first_run_environment_removes_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("SERVICE_PASSWORD", "secret")
    monkeypatch.setenv("CODEROOK_LLM_PROVIDER", "old")
    monkeypatch.setenv("PATH_SAFE_MARKER", "keep")
    home = tmp_path / "home"
    site = tmp_path / "site"

    env = _first_run_environment(home, site)

    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "SERVICE_PASSWORD" not in env
    assert "CODEROOK_LLM_PROVIDER" not in env
    assert env["PATH_SAFE_MARKER"] == "keep"
    assert env["HOME"] == str(home)
    assert env["PYTHONPATH"] == str(site)

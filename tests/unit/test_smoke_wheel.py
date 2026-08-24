from __future__ import annotations

from pathlib import Path

import pytest
from scripts.smoke_wheel import _assert_first_run_status, _first_run_environment


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


# 功能：验证 wheel smoke 接受共享 readiness 的首次未配置输出
# 设计：使用真实 CLI 字段集合并保留无 provider/model/endpoint 断言，防止脚本退回旧 incomplete 合同
def test_first_run_status_accepts_unconfigured_readiness() -> None:
    _assert_first_run_status(
        "status:   unconfigured\n"
        "provider: (none)\n"
        "model:    (none)\n"
        "endpoint: (none)\n"
        "credential: missing\n"
        "validation: not_run\n"
    )


# 功能：验证 wheel smoke 拒绝已废弃的旧首次配置文案
# 设计：只提供历史 incomplete/api-key 输出，确保分发门禁会发现 CLI 与 smoke 合同再次漂移
def test_first_run_status_rejects_legacy_incomplete_output() -> None:
    with pytest.raises(RuntimeError, match="unexpected first-run config status"):
        _assert_first_run_status("status:   incomplete\napi key:  (missing)\n")

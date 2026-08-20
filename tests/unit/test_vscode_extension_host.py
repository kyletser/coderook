from __future__ import annotations

from pathlib import Path

import pytest
from scripts.run_vscode_extension_host_smoke import _extension_environment


# 功能：验证 Extension Host smoke 使用隔离状态、随机端口和显式 API token
# 设计：注入开发机凭据后检查子进程环境，防止远端 smoke 意外依赖维护者配置或真实模型密钥
def test_extension_host_environment_is_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "developer-secret")
    monkeypatch.setenv("CODEROOK_API_PORT", "7438")
    evidence = tmp_path / "evidence.json"

    env = _extension_environment(tmp_path, evidence)

    assert "OPENAI_API_KEY" not in env
    assert env["CODEROOK_API_PORT"] != "7438"
    assert env["CODEROOK_API_TOKEN"] == env["CODEROOK_IPC_TOKEN"]
    assert env["CODEROOK_VSCODE_EVIDENCE_PATH"] == str(evidence)
    assert env["CODEROOK_VSCODE_TEST_BASE_URL"].endswith(
        env["CODEROOK_API_PORT"]
    )


# 功能：验证 distribution job 运行真实 daemon Extension Host smoke 并上传机器可读证据
# 设计：同时锁定 workflow、npm 脚本与 suite 能力断言，避免发行检查退化为仅编译或仅打包 VSIX
def test_distribution_runs_extension_host_smoke() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "distribution.yml").read_text(
        encoding="utf-8"
    )
    package = (root / "editors" / "vscode" / "package.json").read_text(
        encoding="utf-8"
    )
    suite = (
        root / "editors" / "vscode" / "src" / "test" / "suite" / "index.ts"
    ).read_text(encoding="utf-8")

    assert "Real daemon Extension Host smoke" in workflow
    assert "run_vscode_extension_host_smoke.py" in workflow
    assert "artifacts/vscode-extension-host.json" in workflow
    assert '"test:host"' in package
    for capability in ("create_thread", "resume_thread", "open_diff"):
        assert capability in suite

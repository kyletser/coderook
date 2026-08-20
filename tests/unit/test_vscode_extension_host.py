from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.run_vscode_extension_host_smoke import (
    _extension_environment,
    _validate_evidence,
)


# 功能：验证 Extension Host smoke 使用隔离状态、随机端口和显式 API token
# 设计：注入开发机凭据后检查子进程环境，防止远端 smoke 意外依赖维护者配置或真实模型密钥
def test_extension_host_environment_is_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "developer-secret")
    monkeypatch.setenv("CODEROOK_API_PORT", "7438")
    evidence = tmp_path / "evidence.json"

    screenshot = tmp_path / "approval.png"
    env = _extension_environment(tmp_path, evidence, screenshot)

    assert "OPENAI_API_KEY" not in env
    assert env["CODEROOK_API_PORT"] != "7438"
    assert env["CODEROOK_API_TOKEN"] == env["CODEROOK_IPC_TOKEN"]
    assert env["CODEROOK_VSCODE_EVIDENCE_PATH"] == str(evidence)
    assert env["CODEROOK_VSCODE_CAPTURE_APPROVAL"] == "1"
    assert env["CODEROOK_VSCODE_SCREENSHOT_PATH"] == str(screenshot)
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
    assert "artifacts/vscode-approval.png" in workflow
    assert "imagemagick xdotool xvfb" in workflow
    assert '"test:host"' in package
    for capability in (
        "create_thread",
        "resume_thread",
        "open_diff",
        "approval_visual",
    ):
        assert capability in suite


# 功能：验证 Extension Host 视觉证据必须绑定真实 PNG 的大小与 SHA-256
# 设计：写入最小 PNG 签名与匹配 JSON 后校验，再篡改图片断言哈希门禁拒绝
def test_extension_host_evidence_binds_approval_screenshot(tmp_path: Path) -> None:
    screenshot = tmp_path / "approval.png"
    image = b"\x89PNG\r\n\x1a\n" + b"visual-evidence"
    screenshot.write_bytes(image)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "status": "passed",
                "commit": "a" * 40,
                "capabilities": {
                    "activation": True,
                    "create_thread": True,
                    "resume_thread": True,
                    "open_diff": True,
                    "approval_visual": True,
                },
                "approval_screenshot": {
                    "bytes": len(image),
                    "sha256": hashlib.sha256(image).hexdigest(),
                },
            }
        ),
        encoding="utf-8",
    )

    _validate_evidence(
        evidence,
        expected_commit="a" * 40,
        screenshot=screenshot,
    )
    screenshot.write_bytes(image + b"tampered")

    with pytest.raises(RuntimeError, match="hash mismatch"):
        _validate_evidence(
            evidence,
            expected_commit="a" * 40,
            screenshot=screenshot,
        )


# 功能：验证 Extension Host smoke 可按文件路径直接启动并显示帮助
# 设计：使用真实 Python 子进程清除测试进程的包导入偶然性，覆盖 CI 中 scripts 包无法解析的入口
def test_extension_host_smoke_direct_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "run_vscode_extension_host_smoke.py"),
            "--help",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert "Run the VS Code extension" in result.stdout

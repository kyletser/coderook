from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scripts import check_sandbox_boundary

from code_rook.core.authority.models import SandboxCapability


# 功能：验证无 OS 后端的平台生成明确 degraded、禁用 AUTO_REVIEW 的通过报告
# 设计：注入 Windows 能力与临时输出路径，检查逐项结果而不依赖测试主机真实平台
def test_degraded_sandbox_report_is_machine_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "sandbox.json"
    capability = SandboxCapability(
        available=False,
        kind="windows_none",
        reason="no OS isolation backend",
    )
    monkeypatch.setattr(
        check_sandbox_boundary,
        "detect_sandbox_capability",
        lambda: capability,
    )
    monkeypatch.setattr(sys, "argv", ["check_sandbox_boundary.py", "--output", str(output)])

    assert check_sandbox_boundary.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["backend"] == "windows_none"
    assert report["enforced"] is False
    assert report["degraded"] is True
    assert report["gate_passed"] is True
    assert {item["name"] for item in report["checks"]} == {
        "domain_allowlist_fail_closed",
        "degraded_plan_explicit",
        "auto_review_disabled",
    }


# 功能：验证 CI 为三个平台上传各自的沙箱 JSON artifact
# 设计：锁定 runner.os 文件名和 always 上传合同，防止安全门禁退化为仅控制台 PASS 文本
def test_ci_uploads_platform_sandbox_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "--output reports/sandbox-boundary-${{ runner.os }}.json" in workflow
    assert "name: sandbox-boundary-${{ runner.os }}" in workflow
    assert "if: always()" in workflow

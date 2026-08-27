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


# 功能：验证 Windows ACL 手动门禁输出 partial 写边界而不是冒充 full
# 设计：注入成功 capability 与生产探针结果，在任意 CI 平台固定报告字段和四项 Windows 检查
def test_windows_partial_sandbox_report_is_machine_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "sandbox-windows.json"
    capability = SandboxCapability(
        available=True,
        kind="windows_acl",
        reason="restricted-token probe succeeded",
    )
    monkeypatch.setattr(
        check_sandbox_boundary,
        "detect_sandbox_capability",
        lambda: capability,
    )
    monkeypatch.setattr(check_sandbox_boundary, "probe_windows_acl", lambda: True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_sandbox_boundary.py", "--output", str(output)],
    )

    assert check_sandbox_boundary.main() == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["backend"] == "windows_acl"
    assert report["enforced"] is True
    assert report["enforcement"] == "partial"
    assert report["degraded"] is False
    assert report["gate_passed"] is True
    assert {item["name"] for item in report["checks"]} >= {
        "partial_enforcement_reported",
        "workspace_and_readonly_write_boundary",
        "auto_review_disabled",
        "network_and_reads_not_isolated",
    }


# 功能：验证仅手动触发的安全矩阵为三个平台上传沙箱 JSON artifact
# 设计：锁定 workflow_dispatch、runner.os 文件名和 always 上传合同，避免增加日常 CI 邮件
def test_manual_security_workflow_uploads_platform_sandbox_evidence() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "security.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in workflow
    assert "--output reports/sandbox-boundary-${{ runner.os }}.json" in workflow
    assert "name: sandbox-boundary-${{ runner.os }}" in workflow
    assert "if: always()" in workflow

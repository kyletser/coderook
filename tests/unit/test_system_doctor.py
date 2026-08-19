from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from code_rook.cli.commands.doctor import build_system_report, cmd_diagnostic_bundle
from code_rook.core.config import CodeRookConfig


# 功能：验证统一 system doctor 汇总端口、磁盘、sandbox、工具链和 runtime 且不输出 token 正文
# 设计：隔离 HOME 与工作区并在配置中放入诱饵 secret，只检查结构和脱敏布尔状态
def test_system_doctor_report_is_complete_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(workspace)
    config = CodeRookConfig()
    config.api.token = "super-secret-token"

    report = build_system_report(config)
    serialized = json.dumps(report)

    assert {"ports", "sandbox", "tools", "disk", "runtime"} <= report.keys()
    assert report["config"]["api_token_configured"] is True
    assert "super-secret-token" not in serialized


# 功能：验证诊断包需显式确认且日志中的 Bearer/API key 被替换
# 设计：写入两种凭据形态的本地日志，生成 ZIP 后读取成员正文并断言原值不存在
def test_diagnostic_bundle_requires_confirmation_and_redacts_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    state = home / ".coderook"
    state.mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(workspace)
    (state / "core.log").write_text(
        "Authorization: Bearer abc.secret\napi_key=sk-live-secret\n",
        encoding="utf-8",
    )
    target = tmp_path / "diagnostics.zip"

    with pytest.raises(SystemExit, match="requires --yes"):
        cmd_diagnostic_bundle(CodeRookConfig(), target, confirmed=False)
    cmd_diagnostic_bundle(CodeRookConfig(), target, confirmed=True)

    with zipfile.ZipFile(target) as archive:
        log = archive.read("logs/core.log").decode("utf-8")
        names = set(archive.namelist())
    assert {"system-report.json", "logs/core.log"} <= names
    assert "abc.secret" not in log
    assert "sk-live-secret" not in log
    assert "<redacted>" in log

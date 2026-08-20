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


# 功能：验证 Docker 构建包含包元数据声明的许可证文件
# 设计：直接锁定 COPY 合同，避免 clean context 到 Hatchling metadata 阶段才发现 LICENSE 缺失
def test_docker_builder_copies_license_metadata() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml README.md LICENSE ./" in dockerfile


# 功能：验证 portable 构建显式允许安装到已复制的 uv-managed Python 副本
# 设计：锁定 break-system-packages 参数，防止新版 uv 的 externally-managed 保护再次阻断打包
def test_portable_builder_allows_managed_runtime_copy() -> None:
    root = Path(__file__).resolve().parents[2]
    builder = (root / "scripts" / "build_windows_portable.ps1").read_text(
        encoding="utf-8"
    )

    assert "--directory ([System.IO.Path]::GetTempPath())" in builder
    assert "python find --managed-python 3.12" in builder
    assert "--python $portablePython --break-system-packages" in builder

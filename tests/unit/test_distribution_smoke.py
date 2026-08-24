from __future__ import annotations

import os
from pathlib import Path

import pytest
from scripts.build_portable import _current_host_target, _require_native_target, _write_launcher
from scripts.generate_package_manifests import _homebrew_formula, _scoop_manifest
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


# 功能：验证 Docker 与五平台 portable 矩阵均运行真实 installed-runtime smoke
# 设计：检查容器及按平台分支的调用，并锁定五种 target，防止发行门禁退化回只打印版本
def test_distribution_workflow_smokes_container_and_portable_runtime() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github" / "workflows" / "distribution.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("smoke_installed_runtime.py") == 3
    assert "Container zero-credential Core, ping and TUI smoke" in workflow
    assert "Portable zero-credential Core, ping and TUI smoke" in workflow
    for target in (
        "windows-x86_64",
        "linux-x86_64",
        "linux-arm64",
        "macos-x86_64",
        "macos-arm64",
    ):
        assert target in workflow
    assert "github.event_name == 'workflow_call'" not in workflow
    assert "default: all" in workflow


# 功能：验证 portable 构建拒绝把本机 runtime 标记成另一操作系统或 CPU target
# 设计：固定 Linux x64 宿主后同时检查规范化结果和 arm64 拒绝路径，避免真实复制 Python 才发现误标
def test_portable_builder_fails_closed_on_host_target_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scripts.build_portable.platform.system", lambda: "Linux")
    monkeypatch.setattr("scripts.build_portable.platform.machine", lambda: "x86_64")

    assert _current_host_target() == "linux-x86_64"
    _require_native_target("linux-x86_64")
    with pytest.raises(RuntimeError, match="does not match native build host"):
        _require_native_target("linux-arm64")


# 功能：验证 Docker 构建包含包元数据声明的许可证文件
# 设计：直接锁定 COPY 合同，避免 clean context 到 Hatchling metadata 阶段才发现 LICENSE 缺失
def test_docker_builder_copies_license_metadata() -> None:
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY pyproject.toml README.md LICENSE ./" in dockerfile


# 功能：验证 portable 构建显式允许安装到复制的 uv-managed Python 并生成可移动启动器
# 设计：锁定通用构建器的 managed Python、安装参数和非模块空执行入口，防止目录移动后失效
def test_portable_builder_allows_managed_runtime_copy() -> None:
    root = Path(__file__).resolve().parents[2]
    builder = (root / "scripts" / "build_portable.py").read_text(
        encoding="utf-8"
    )

    assert '"python", "find", "--managed-python", "3.12"' in builder
    assert '"--break-system-packages"' in builder
    assert "from code_rook.cli.main import main" in builder


# 功能：验证生成的 Windows 与 POSIX 启动器都调用真实 CLI main 且使用相对 runtime
# 设计：只生成小型启动文件而不复制 Python，检查移动目录所需的根路径计算和入口调用
def test_portable_launchers_are_relative_and_executable(tmp_path: Path) -> None:
    windows_root = tmp_path / "windows"
    posix_root = tmp_path / "posix"
    windows_root.mkdir()
    posix_root.mkdir()

    windows = _write_launcher(windows_root, "windows-x86_64")
    posix = _write_launcher(posix_root, "linux-x86_64")

    assert "%~dp0" in windows.read_text(encoding="utf-8")
    assert "from code_rook.cli.main import main" in windows.read_text(encoding="utf-8")
    assert "dirname" in posix.read_text(encoding="utf-8")
    if os.name != "nt":
        assert posix.stat().st_mode & 0o111


# 功能：验证发行 checksum 可生成 Homebrew 多架构 Formula 与 Scoop x64 manifest
# 设计：使用固定摘要构造全部资产，检查 URL、架构分支、版本和 hash 不依赖外部网络
def test_package_manager_manifests_bind_release_checksums() -> None:
    checksums = {
        "coderook-macos-arm64.tar.gz": "a" * 64,
        "coderook-macos-x86_64.tar.gz": "b" * 64,
        "coderook-linux-arm64.tar.gz": "c" * 64,
        "coderook-linux-x86_64.tar.gz": "d" * 64,
        "coderook-windows-x86_64.zip": "e" * 64,
    }

    formula = _homebrew_formula("1.0.0", "https://example.test/v1.0.0", checksums)
    scoop = _scoop_manifest("1.0.0", "https://example.test/v1.0.0", checksums)

    assert "Hardware::CPU.arm?" in formula
    assert checksums["coderook-macos-arm64.tar.gz"] in formula
    assert '"version": "1.0.0"' in scoop
    assert checksums["coderook-windows-x86_64.zip"] in scoop
    assert "releases/download/v$version/coderook-windows-x86_64.zip" in scoop

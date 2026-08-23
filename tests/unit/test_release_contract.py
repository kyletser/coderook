from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts.check_release_contract import (
    python_version_for_tag,
    validate_release_contract,
)
from scripts.generate_release_manifest import generate_release_manifest

_ROOT = Path(__file__).resolve().parents[2]


# 功能：验证稳定版与 SemVer 预发行 tag 映射到正确 PEP 440 包版本
# 设计：覆盖 alpha/beta/rc 和非法裸 beta，固定 Python 与 VSIX 使用不同规范时的唯一转换规则
def test_release_tag_maps_to_python_version() -> None:
    assert python_version_for_tag("v1.2.3") == "1.2.3"
    assert python_version_for_tag("v1.2.3-alpha.2") == "1.2.3a2"
    assert python_version_for_tag("v1.2.3-beta.4") == "1.2.3b4"
    assert python_version_for_tag("v1.2.3-rc.1") == "1.2.3rc1"
    with pytest.raises(ValueError, match="tag must match"):
        python_version_for_tag("v1.2.3-beta")


# 功能：验证当前未发布的 0.1.0 不能被版本字符串伪装成已存在的 tag
# 设计：直接读取真实 Changelog，要求缺少带日期版本标题时 fail closed，保持与远端无 tag 的事实一致
def test_unpublished_current_version_is_not_a_valid_release_contract() -> None:
    with pytest.raises(ValueError, match=r"no dated \[0\.1\.0\] release heading"):
        validate_release_contract("v0.1.0", _ROOT)


# 功能：验证未发布候选在评分卡 NO-GO 时同时报告发布资格失败
# 设计：开启 require_go 并匹配 readiness 错误，确保版本合同其他失败也不能掩盖 NO-GO 门禁
def test_release_contract_requires_scorecard_go_for_tag_publish() -> None:
    with pytest.raises(ValueError, match="requires GO"):
        validate_release_contract("v0.1.0", _ROOT, require_go=True)


# 功能：验证发行 manifest、SHA256SUMS、类型与排除签名包的确定性行为
# 设计：创建四类小资产和一个签名 bundle，逐项重算哈希并确认 bundle 不形成自引用校验
def test_generate_release_manifest_hashes_primary_assets(tmp_path: Path) -> None:
    assets = {
        "coderook-1.0.0.whl": b"wheel",
        "coderook-1.0.0.tar.gz": b"sdist",
        "coderook-windows-portable.zip": b"portable",
        "coderook-vscode.vsix": b"vsix",
        "coderook-wheel.spdx.json": b"{}",
        "old.sigstore.json": b"signature",
    }
    for name, content in assets.items():
        (tmp_path / name).write_bytes(content)

    manifest = generate_release_manifest(tmp_path, "1.0.0")

    names = [entry["name"] for entry in manifest["artifacts"]]
    assert "old.sigstore.json" not in names
    assert "coderook-1.0.0.whl" in names
    assert "coderook-wheel.spdx.json" in names
    checksum_lines = (tmp_path / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected = hashlib.sha256(b"wheel").hexdigest()
    assert f"{expected} *coderook-1.0.0.whl" in checksum_lines
    assert any(line.endswith("*release-manifest.json") for line in checksum_lines)
    encoded = json.loads((tmp_path / "release-manifest.json").read_text(encoding="utf-8"))
    assert encoded == manifest


# 功能：验证 tag workflow 把质量、供应链与最终发布按 fail-closed 顺序连接
# 设计：检查仓库内真实 YAML 的关键权限、动作和 needs，不依赖 GitHub runner 也能阻止安全步骤被误删
def test_release_workflow_contains_supply_chain_gates() -> None:
    release = (_ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    distribution = (
        _ROOT / ".github" / "workflows" / "distribution.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_call:" in distribution
    assert "push:\n    tags:" not in distribution
    assert "--require-go" in release
    assert "needs: [distribution-gate, validate, container]" in release
    assert "id-token: write" in release
    assert "actions/attest@v4" in release
    assert "subject-checksums: release/SHA256SUMS" in release
    assert "anchore/sbom-action" in release
    assert "cosign sign-blob" in release
    assert "cosign verify-blob" in release
    assert "gh release create" in release

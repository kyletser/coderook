from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_rook.core.sandbox.windows_acl_runner import capability_sid, probe


# 功能：验证 capability SID 对规范路径确定、对用途隔离且保持 Windows SID 格式
# 设计：同一路径重复派生并切换 domain，对比结果防止工作区与私有临时目录共享写身份
def test_windows_capability_sid_is_stable_and_domain_separated(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = capability_sid(workspace, domain="workspace-write")
    repeated = capability_sid(workspace, domain="workspace-write")
    private_temp = capability_sid(workspace, domain="private-temp")

    assert first == repeated
    assert first.startswith("S-1-4-")
    assert private_temp != first


@pytest.mark.skipif(os.name != "nt", reason="Windows Restricted Token only")
# 功能：验证真实 Windows runner 允许工作区写入并拒绝外部写入和 read-only 写入
# 设计：复用生产探针经过真实 ACL、CreateRestrictedToken、CreateProcessAsUser 与 Job Object 全链路
def test_windows_acl_runner_enforces_real_write_boundaries() -> None:
    assert probe() is True

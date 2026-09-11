from __future__ import annotations

from pathlib import Path

from code_rook.core.authority import WorkspaceTrust, WorkspaceTrustStore


# 功能：验证工作区信任按项目隔离持久化并可显式撤销
# 设计：复用同一存储实例写入两个临时目录，重建实例后核对精确路径和默认值
def test_workspace_trust_is_persistent_and_project_scoped(tmp_path: Path) -> None:
    path = tmp_path / "state" / "workspace-trust.json"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store = WorkspaceTrustStore(path)

    store.set(first, WorkspaceTrust.TRUSTED)

    reloaded = WorkspaceTrustStore(path)
    assert reloaded.get(first) == WorkspaceTrust.TRUSTED
    assert reloaded.get(second) == WorkspaceTrust.UNTRUSTED

    reloaded.set(first, WorkspaceTrust.UNTRUSTED)
    assert WorkspaceTrustStore(path).get(first) == WorkspaceTrust.UNTRUSTED

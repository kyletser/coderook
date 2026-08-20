from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.run_upgrade_preflight import (
    _DEFAULT_BASELINE_REF,
    _resolve_commit,
    _runtime_schema,
    _tree_sha256,
    _validate_commit_order,
    _version_triplet,
    run_preflight,
)

_ROOT = Path(__file__).resolve().parents[2]


# 功能：验证默认安装基线是当前 HEAD 的真实祖先且版本比较只接受稳定三段版本
# 设计：直接查询仓库历史并覆盖相等版本拒绝，防止 preflight 用同一提交或模糊版本伪造升级
def test_upgrade_preflight_baseline_identity_and_version_order() -> None:
    baseline = _resolve_commit(_DEFAULT_BASELINE_REF)
    candidate = _resolve_commit("HEAD")

    _validate_commit_order(baseline, candidate)
    assert _version_triplet("0.1.0") > _version_triplet("0.0.1")
    with pytest.raises(RuntimeError, match="stable x.y.z"):
        _version_triplet("0.2.0-beta.1")


# 功能：验证备份目录哈希同时绑定相对路径和文件内容且不依赖创建顺序
# 设计：以相反顺序写入两个文件后重建同构目录，再修改路径，区分内容集合与可恢复树身份
def test_upgrade_backup_tree_digest_is_deterministic_and_path_bound(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, order in ((first, ("b", "a")), (second, ("a", "b"))):
        root.mkdir()
        for name in order:
            (root / f"{name}.json").write_text(name, encoding="utf-8")

    assert _tree_sha256(first) == _tree_sha256(second)
    (second / "b.json").rename(second / "nested.json")
    assert _tree_sha256(first) != _tree_sha256(second)


# 功能：验证读取 runtime schema 后立即释放 SQLite 文件句柄
# 设计：查询 user_version 后直接删除数据库，覆盖 Windows 升级回滚无法替换锁定文件的真实边界
def test_upgrade_runtime_schema_read_releases_database(tmp_path: Path) -> None:
    home = tmp_path / "home"
    database = home / ".coderook" / "runtime.db"
    database.parent.mkdir(parents=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA user_version = 3")
    finally:
        connection.close()

    assert _runtime_schema(home) == 3
    database.unlink()
    assert not database.exists()


# 功能：验证相同 baseline/candidate 在构建 wheel 前即被拒绝且不写成功证据
# 设计：替换 commit 解析为同一身份，确保昂贵构建之前 fail fast，并检查不存在误导性的报告文件
def test_upgrade_preflight_rejects_identical_commits_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "upgrade.json"
    monkeypatch.setattr(
        "scripts.run_upgrade_preflight._resolve_commit",
        lambda _ref: "a" * 40,
    )

    with pytest.raises(RuntimeError, match="must differ"):
        run_preflight(
            baseline_ref="baseline",
            evidence=evidence,
            allow_dirty=True,
        )

    assert not evidence.exists()


# 功能：验证 Distribution 可聚焦运行三平台安装态升级与备份回滚 preflight
# 设计：锁定显式 baseline、三平台矩阵、完整 Git 历史和逐平台 artifact，避免退化为源码级迁移单测
def test_distribution_declares_cross_platform_upgrade_preflight() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "distribution.yml").read_text(
        encoding="utf-8"
    )

    assert f"default: {_DEFAULT_BASELINE_REF}" in workflow
    assert "inputs.target == 'upgrade'" in workflow
    assert "os: [ubuntu-latest, windows-latest, macos-latest]" in workflow
    assert "fetch-depth: 0" in workflow
    assert "run_upgrade_preflight.py" in workflow
    assert "upgrade-preflight-${{ runner.os }}" in workflow


# 功能：验证升级 preflight 可按脚本路径直接启动并公开 tag 严格模式
# 设计：使用真实子进程显示帮助，覆盖 CI 中 scripts 包解析和发行前 require-baseline-tag 入口
def test_upgrade_preflight_direct_entrypoint() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts" / "run_upgrade_preflight.py"),
            "--help",
        ],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, result.stderr
    assert "--require-baseline-tag" in result.stdout

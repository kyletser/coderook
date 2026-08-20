from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.validate_upgrade_preflight_reports import validate_reports


# 构造单平台升级报告并允许测试覆盖身份字段
def _report(platform: str) -> dict[str, object]:
    baseline_thread = f"baseline-{platform}"
    return {
        "schema_version": 1,
        "status": "passed",
        "platform": platform,
        "baseline": {
            "commit": "a" * 40,
            "version": "0.0.1",
        },
        "candidate": {
            "commit": "b" * 40,
            "dirty": False,
            "version": "0.1.0",
        },
        "backup_sha256": platform * 8,
        "phases": {
            "baseline": {
                "thread_id": baseline_thread,
                "thread_count": 1,
                "runtime_schema": 3,
            },
            "upgrade": {
                "retained_thread_id": baseline_thread,
                "created_thread_id": f"candidate-{platform}",
                "thread_count": 2,
                "runtime_schema": 3,
            },
            "rollback": {
                "version": "0.0.1",
                "restored_thread_id": baseline_thread,
                "thread_count": 1,
                "runtime_schema": 3,
                "backup_hash_matches": True,
            },
        },
    }


# 写入三平台报告并返回路径列表
def _write_reports(tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for platform in ("linux", "win32", "darwin"):
        path = tmp_path / f"{platform}.json"
        path.write_text(json.dumps(_report(platform)), encoding="utf-8")
        paths.append(path)
    return paths


# 功能：验证聚合器接受身份一致且状态往返闭合的三平台报告
# 设计：每个平台使用不同 thread 和备份哈希，证明聚合只要求不变量一致而不伪造跨 OS 字节相同
def test_upgrade_aggregate_accepts_three_platform_roundtrip(tmp_path: Path) -> None:
    result = validate_reports(
        _write_reports(tmp_path),
        expected_commit="b" * 40,
        expected_baseline_commit="a" * 40,
    )

    assert result["status"] == "passed"
    assert result["platforms"] == ["darwin", "linux", "win32"]
    assert len(result["reports"]) == 3


# 功能：验证缺平台、脏候选和升级后 thread 数不增均阻断聚合门禁
# 设计：在同一报告上组合三种破坏，断言错误文本逐项出现，避免聚合只检查文件数量
def test_upgrade_aggregate_rejects_incomplete_or_tampered_reports(
    tmp_path: Path,
) -> None:
    paths = _write_reports(tmp_path)
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    payload["platform"] = "win32"
    payload["candidate"]["dirty"] = True
    payload["phases"]["upgrade"]["thread_count"] = 1
    paths[0].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as raised:
        validate_reports(
            paths,
            expected_commit="b" * 40,
            expected_baseline_commit="a" * 40,
        )

    message = str(raised.value)
    assert "platforms must be exactly" in message
    assert "candidate worktree was dirty" in message
    assert "thread count must increase by one" in message

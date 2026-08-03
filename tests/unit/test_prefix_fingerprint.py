from __future__ import annotations

import json

from code_rook.core.prefix_fingerprint import PrefixFingerprintTracker


# 功能：验证相同稳定前缀重复观察不会报告变化
# 设计：连续传入完全相同的三类来源，比较总指纹并断言第二次变化集为空
def test_prefix_fingerprint_is_stable_for_same_sources() -> None:
    tracker = PrefixFingerprintTracker()

    first = tracker.observe(
        system_prompt="system",
        tool_catalog=b"catalog",
        stable_memory="memory",
    )
    second = tracker.observe(
        system_prompt="system",
        tool_catalog=b"catalog",
        stable_memory="memory",
    )

    assert first.digest == second.digest
    assert set(first.changed_sources) == {
        "stable_memory",
        "system_prompt",
        "tool_catalog",
    }
    assert second.changed_sources == ()


# 功能：验证单个来源变化时收据精确指出该来源
# 设计：建立基线后分别修改 system、catalog、memory，并在每次后恢复其余来源
def test_prefix_fingerprint_identifies_changed_source() -> None:
    tracker = PrefixFingerprintTracker()
    tracker.observe(system_prompt="s", tool_catalog=b"t", stable_memory="m")

    system = tracker.observe(system_prompt="s2", tool_catalog=b"t", stable_memory="m")
    catalog = tracker.observe(system_prompt="s2", tool_catalog=b"t2", stable_memory="m")
    memory = tracker.observe(system_prompt="s2", tool_catalog=b"t2", stable_memory="m2")

    assert system.changed_sources == ("system_prompt",)
    assert catalog.changed_sources == ("tool_catalog",)
    assert memory.changed_sources == ("stable_memory",)


# 功能：验证前缀收据只含哈希与来源名，不泄露敏感 prompt 正文
# 设计：使用唯一 secret 作为三类输入并序列化全部收据字段，检查原文完全不存在
def test_prefix_fingerprint_receipt_does_not_expose_source_content() -> None:
    tracker = PrefixFingerprintTracker()
    secrets = ("secret-system-body", "secret-tool-schema", "secret-memory-note")

    receipt = tracker.observe(
        system_prompt=secrets[0],
        tool_catalog=secrets[1].encode(),
        stable_memory=secrets[2],
    )
    serialized = json.dumps(receipt.__dict__, sort_keys=True)

    assert all(secret not in serialized for secret in secrets)

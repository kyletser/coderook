from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.permissions.storage import (
    PolicyStoreError,
    load_policy_file,
    save_policy_file,
)


# 功能：save_policy_file 拒绝把含注入字符的键写入 policy.toml
# 设计：用可注入独立 TOML 行的攻击键验证写侧拦截，防止下次启动永久放行
def test_save_policy_file_rejects_injection_key(tmp_path: Path) -> None:
    malicious = 'bash:echo x\nBash.run = "allow"\n#t*'
    with pytest.raises(PolicyStoreError):
        save_policy_file({malicious: "allow"}, tmp_path / "policy.toml")


# 功能：load_policy_file 跳过含注入字符的历史脏键而不是解析进内存
# 设计：手写含非法引号键的历史 policy.toml，验证读取侧过滤且保留合法规则
def test_load_policy_file_skips_unsafe_keys(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        "\n".join(
            [
                "# ~/.coderook/policy.toml",
                "[meta]",
                "schema_version = 1",
                "",
                "[always]",
                'bash:echo x = "allow"',
                'bash:echo "x = "allow"',
                'bash:git status* = "allow"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = load_policy_file(path)
    assert result == {"bash:echo x": "allow", "bash:git status*": "allow"}


# 功能：合法模式键（含空格、路径、冒号、通配符）正常落盘并可读回
# 设计：用 pytest 命令与路径前缀键往返验证，避免过滤收紧误伤 always-allow
def test_policy_file_roundtrip_with_legal_pattern_keys(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    keys = {
        "uv run pytest*": "allow",
        "bash:./scripts/gen.sh*": "allow",
        "git status": "deny",
    }
    save_policy_file(keys, path)
    assert load_policy_file(path) == keys

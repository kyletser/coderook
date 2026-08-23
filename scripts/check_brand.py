#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_ALLOWED = {
    Path("scripts/check_brand.py"),
    Path("src/code_rook/core/state_migration.py"),
    Path("tests/unit/test_state_migration.py"),
}
_TEXT_SUFFIXES = {
    "",
    ".example",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
_LEGACY_PATTERNS = [
    re.compile("kyle" + "claude", re.IGNORECASE),
    re.compile("kyle" + "_claude", re.IGNORECASE),
    re.compile("kyle" + "-claude", re.IGNORECASE),
    re.compile("kyle" + "-core", re.IGNORECASE),
    re.compile("kyle" + "-tui", re.IGNORECASE),
    re.compile("kyle" + "_", re.IGNORECASE),
    re.compile(r"\." + "kyle" + r"\b", re.IGNORECASE),
    re.compile(r"\b" + "kyle" + r"\b", re.IGNORECASE),
]


# 判断仓库文件是否属于需要执行品牌扫描的文本文件
def _should_scan(path: Path) -> bool:
    relative = path.relative_to(_ROOT)
    return (
        path.is_file()
        and relative not in _ALLOWED
        and not any(part in _SKIP_DIRS for part in relative.parts)
        and path.suffix.lower() in _TEXT_SUFFIXES
    )


# 扫描旧品牌标识并返回带文件和行号的命中列表
def find_legacy_brand_references() -> list[str]:
    findings: list[str] = []
    for path in _ROOT.rglob("*"):
        if not _should_scan(path):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            if any(pattern.search(line) for pattern in _LEGACY_PATTERNS):
                relative = path.relative_to(_ROOT).as_posix()
                findings.append(f"{relative}:{line_number}: {line.strip()}")
    return findings


# 作为 CI 门禁运行品牌扫描，发现非兼容层残留时以非零状态退出
def main() -> None:
    findings = find_legacy_brand_references()
    if findings:
        raise SystemExit(
            "Legacy brand references found outside migration compatibility files:\n"
            + "\n".join(findings)
        )
    print("CodeRook brand check passed.")


if __name__ == "__main__":
    main()

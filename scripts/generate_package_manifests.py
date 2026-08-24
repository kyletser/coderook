#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


# 读取标准 SHA256SUMS 并返回以资产名索引的摘要
def _read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        checksums[parts[1].removeprefix("*")] = parts[0].lower()
    return checksums


# 生成可提交到 Homebrew tap 的多平台 Formula
def _homebrew_formula(version: str, base_url: str, checksums: dict[str, str]) -> str:
    assets = {
        "macos-arm64": "coderook-macos-arm64.tar.gz",
        "macos-x86_64": "coderook-macos-x86_64.tar.gz",
        "linux-arm64": "coderook-linux-arm64.tar.gz",
        "linux-x86_64": "coderook-linux-x86_64.tar.gz",
    }
    missing = [asset for asset in assets.values() if asset not in checksums]
    if missing:
        raise ValueError(f"missing portable checksums: {', '.join(missing)}")
    return f'''class Coderook < Formula
  desc "Local-first, auditable TUI coding agent"
  homepage "https://github.com/kyletser/coderook"
  version "{version}"

  on_macos do
    if Hardware::CPU.arm?
      url "{base_url}/{assets['macos-arm64']}"
      sha256 "{checksums[assets['macos-arm64']]}"
    else
      url "{base_url}/{assets['macos-x86_64']}"
      sha256 "{checksums[assets['macos-x86_64']]}"
    end
  end

  on_linux do
    if Hardware::CPU.arm?
      url "{base_url}/{assets['linux-arm64']}"
      sha256 "{checksums[assets['linux-arm64']]}"
    else
      url "{base_url}/{assets['linux-x86_64']}"
      sha256 "{checksums[assets['linux-x86_64']]}"
    end
  end

  def install
    target = if OS.mac?
      Hardware::CPU.arm? ? "macos-arm64" : "macos-x86_64"
    else
      Hardware::CPU.arm? ? "linux-arm64" : "linux-x86_64"
    end
    libexec.install Dir["coderook-#{{target}}/*"]
    bin.install_symlink libexec/"coderook"
  end

  test do
    assert_match version.to_s, shell_output("#{{bin}}/coderook --version")
  end
end
'''


# 生成可放入 Scoop bucket 的 Windows x64 manifest
def _scoop_manifest(version: str, base_url: str, checksums: dict[str, str]) -> str:
    asset = "coderook-windows-x86_64.zip"
    if asset not in checksums:
        raise ValueError(f"missing portable checksum: {asset}")
    payload = {
        "version": version,
        "description": "Local-first, auditable TUI coding agent",
        "homepage": "https://github.com/kyletser/coderook",
        "license": "MIT",
        "architecture": {
            "64bit": {
                "url": f"{base_url}/{asset}",
                "hash": checksums[asset],
            }
        },
        "extract_dir": "coderook-windows-x86_64",
        "bin": [["coderook.cmd", "coderook"]],
        "checkver": {"github": "https://github.com/kyletser/coderook"},
        "autoupdate": {
            "architecture": {
                "64bit": {
                    "url": (
                        "https://github.com/kyletser/coderook/releases/download/"
                        "v$version/coderook-windows-x86_64.zip"
                    )
                }
            }
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


# 解析版本与发行目录并写出 Homebrew、Scoop 两种安装清单
def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Homebrew and Scoop manifests.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", default="kyletser/coderook")
    args = parser.parse_args()
    checksums = _read_checksums(args.input / "SHA256SUMS")
    tag = args.version if args.version.startswith("v") else f"v{args.version}"
    base_url = f"https://github.com/{args.repository}/releases/download/{tag}"
    (args.input / "coderook.rb").write_text(
        _homebrew_formula(args.version.removeprefix("v"), base_url, checksums),
        encoding="utf-8",
        newline="\n",
    )
    (args.input / "coderook-scoop.json").write_text(
        _scoop_manifest(args.version.removeprefix("v"), base_url, checksums),
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

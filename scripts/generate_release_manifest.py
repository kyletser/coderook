#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

_EXCLUDED_NAMES = {
    "SHA256SUMS",
    "release-manifest.json",
}
_EXCLUDED_SUFFIXES = (".sigstore.json",)


# 流式计算大体积发行产物的 SHA-256
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


# 根据扩展名给发行资产标记可读类型
def _artifact_kind(path: Path) -> str:
    name = path.name
    if name.startswith("coderook-windows-") and name.endswith(".zip"):
        return "self-contained-windows"
    if name.startswith("coderook-linux-") and name.endswith(".tar.gz"):
        return "self-contained-linux"
    if name.startswith("coderook-macos-") and name.endswith(".tar.gz"):
        return "self-contained-macos"
    if name.endswith(".whl"):
        return "python-wheel"
    if name.endswith(".tar.gz"):
        return "python-sdist"
    if name.endswith(".vsix"):
        return "vscode-extension"
    if name.endswith("portable.zip"):
        return "windows-portable"
    if name.endswith(".spdx.json"):
        return "sbom-spdx-json"
    if name == "container-image.json":
        return "container-reference"
    if name == "release-contract.json":
        return "release-contract"
    return "release-metadata"


# 枚举需写入 manifest 和 SHA256SUMS 的全部普通发行文件
def _artifacts(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in _EXCLUDED_NAMES
        and not path.name.endswith(_EXCLUDED_SUFFIXES)
    )


# 生成机器可读 manifest，再把它与全部资产写入标准 SHA256SUMS
def generate_release_manifest(root: Path, version: str) -> dict[str, Any]:
    root = root.resolve()
    artifacts = _artifacts(root)
    entries = [
        {
            "name": path.relative_to(root).as_posix(),
            "kind": _artifact_kind(path),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in artifacts
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "version": version,
        "artifacts": entries,
    }
    manifest_path = root / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksum_paths = [*artifacts, manifest_path]
    checksum_lines = [
        f"{_sha256(path)} *{path.relative_to(root).as_posix()}"
        for path in checksum_paths
    ]
    (root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


# 解析资产目录与版本并写出可验证发行元数据
def main() -> int:
    parser = argparse.ArgumentParser(description="Generate CodeRook release metadata.")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if not args.input.is_dir():
        raise SystemExit(f"release input directory does not exist: {args.input}")
    manifest = generate_release_manifest(args.input, args.version)
    print(f"Release manifest: {len(manifest['artifacts'])} artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

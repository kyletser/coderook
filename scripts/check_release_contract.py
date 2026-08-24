#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from code_rook.core.compatibility import (
    HTTP_API_VERSION,
    RUNTIME_EVENT_SCHEMA_VERSION,
    STREAM_JSON_SCHEMA_VERSIONS,
)

_ROOT = Path(__file__).resolve().parent.parent
_TAG_RE = re.compile(
    r"^v(?P<base>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<stage>alpha|beta|rc)\.(?P<number>0|[1-9]\d*))?$"
)


# 将公开 SemVer tag 转换成 Python/PEP 440 包版本
def python_version_for_tag(tag: str) -> str:
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError("tag must match vMAJOR.MINOR.PATCH[-alpha|beta|rc.N]")
    version = f"{match.group('base')}.{match.group('minor')}.{match.group('patch')}"
    stage = match.group("stage")
    if stage is None:
        return version
    pep_stage = {"alpha": "a", "beta": "b", "rc": "rc"}[stage]
    return f"{version}{pep_stage}{match.group('number')}"


# 读取 pyproject、Python 包和 VSIX 的三个发行版本
def _declared_versions(root: Path) -> dict[str, str]:
    with (root / "pyproject.toml").open("rb") as stream:
        pyproject_version = str(tomllib.load(stream)["project"]["version"])
    package_text = (root / "src" / "code_rook" / "__init__.py").read_text(
        encoding="utf-8"
    )
    package_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', package_text)
    if package_match is None:
        raise ValueError("src/code_rook/__init__.py does not declare __version__")
    vscode = json.loads(
        (root / "editors" / "vscode" / "package.json").read_text(encoding="utf-8")
    )
    return {
        "pyproject": pyproject_version,
        "python_package": package_match.group(1),
        "vscode": str(vscode["version"]),
    }


# 返回当前 Git commit，非仓库测试夹具回退 unknown
def _git_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 校验 tag、包、扩展、Changelog 和公共协议版本并返回发行清单
def validate_release_contract(
    tag: str,
    root: Path = _ROOT,
    *,
    require_go: bool = False,
    require_channel_readiness: bool = False,
) -> dict[str, Any]:
    python_version = python_version_for_tag(tag)
    semver = tag.removeprefix("v")
    versions = _declared_versions(root)
    issues: list[str] = []
    for source in ("pyproject", "python_package"):
        if versions[source] != python_version:
            issues.append(
                f"{source} version {versions[source]!r} != expected {python_version!r}"
            )
    if versions["vscode"] != semver:
        issues.append(f"vscode version {versions['vscode']!r} != tag version {semver!r}")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if re.search(rf"^## \[{re.escape(semver)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE) is None:
        issues.append(f"CHANGELOG.md has no dated [{semver}] release heading")
    if re.search(rf"^\[{re.escape(semver)}\]: https://github\.com/.+/releases/tag/{re.escape(tag)}$", changelog, re.MULTILINE) is None:
        issues.append(f"CHANGELOG.md has no [{semver}] release link for {tag}")
    if f"compare/{tag}...HEAD" not in changelog:
        issues.append(f"CHANGELOG.md Unreleased link does not start at {tag}")
    compatibility = (root / "docs" / "reference" / "COMPATIBILITY.md").read_text(
        encoding="utf-8"
    )
    if f"`{HTTP_API_VERSION}`" not in compatibility:
        issues.append(f"compatibility doc does not declare HTTP {HTTP_API_VERSION}")
    wire_protocol = "docs/reference/WIRE_PROTOCOL.md"
    if not (root / wire_protocol).is_file():
        issues.append(f"{wire_protocol} is missing")
    scorecard = (root / "docs" / "status" / "RELEASE_SCORECARD.md").read_text(
        encoding="utf-8"
    )
    scorecard_match = re.search(r"候选状态：\*\*(NO-GO|GO)", scorecard)
    readiness = scorecard_match.group(1) if scorecard_match is not None else "UNKNOWN"
    if require_go and readiness != "GO":
        issues.append(f"release scorecard is {readiness}; a tag release requires GO")
    prerelease = "-" in semver
    if require_channel_readiness:
        if not prerelease and readiness != "GO":
            issues.append(
                f"release scorecard is {readiness}; a stable tag release requires GO"
            )
        elif prerelease and readiness not in {"GO", "NO-GO"}:
            issues.append(
                "release scorecard has no recognized candidate status; "
                "a prerelease requires an explicit GO or NO-GO"
            )
    if issues:
        raise ValueError("release contract failed:\n- " + "\n- ".join(issues))
    return {
        "schema_version": 1,
        "tag": tag,
        "version": semver,
        "python_version": python_version,
        "prerelease": prerelease,
        "commit": _git_commit(root),
        "release_readiness": readiness,
        "protocols": {
            "http_api": HTTP_API_VERSION,
            "runtime_event_schema": RUNTIME_EVENT_SCHEMA_VERSION,
            "stream_json_schemas": list(STREAM_JSON_SCHEMA_VERSIONS),
            "ipc_contract": wire_protocol,
        },
    }


# 解析 tag 并可选写出供 Release 页面分发的 JSON 清单
def main() -> int:
    parser = argparse.ArgumentParser(description="Validate CodeRook release versions.")
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME", ""))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-go", action="store_true")
    parser.add_argument("--require-channel-readiness", action="store_true")
    args = parser.parse_args()
    if not args.tag:
        raise SystemExit("--tag or GITHUB_REF_NAME is required")
    try:
        manifest = validate_release_contract(
            args.tag,
            require_go=args.require_go,
            require_channel_readiness=args.require_channel_readiness,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(
        f"Release contract passed: {manifest['tag']} "
        f"(HTTP {HTTP_API_VERSION}, stream-json {STREAM_JSON_SCHEMA_VERSIONS})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

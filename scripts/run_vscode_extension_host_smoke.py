#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.smoke_installed_runtime import (  # noqa: E402
    _wait_until_listening,
    first_run_environment,
)

_EXTENSION = _ROOT / "editors" / "vscode"


# 解析 Extension Host smoke 的命令行参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the VS Code extension against an isolated CodeRook daemon."
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--evidence",
        type=Path,
        default=_ROOT / "artifacts" / "vscode-extension-host.json",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=_ROOT / "artifacts" / "vscode-approval.png",
    )
    return parser.parse_args()


# 返回当前 Git commit，无法读取时保留 unknown 而不伪造身份
def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# 构造连接隔离 daemon 的 Extension Host 子进程环境
def _extension_environment(
    home: Path,
    evidence: Path,
    screenshot: Path | None = None,
) -> dict[str, str]:
    env = first_run_environment(home)
    api_token = env["CODEROOK_IPC_TOKEN"]
    env.update(
        {
            "CODEROOK_API_TOKEN": api_token,
            "CODEROOK_VSCODE_TEST_BASE_URL": (
                f"http://127.0.0.1:{env['CODEROOK_API_PORT']}"
            ),
            "CODEROOK_VSCODE_EVIDENCE_PATH": str(evidence),
            "CODEROOK_VSCODE_TEST_MODE": "1",
            "CODEROOK_VSCODE_TEST_COMMIT": _git_commit(),
            "CODEROOK_VSCODE_WORKSPACE": str(_ROOT),
        }
    )
    if screenshot is not None:
        env.update(
            {
                "CODEROOK_VSCODE_CAPTURE_APPROVAL": "1",
                "CODEROOK_VSCODE_SCREENSHOT_PATH": str(screenshot),
            }
        )
    return env


# 选择当前平台可用的 npm 命令
def _npm_command() -> str:
    command = "npm.cmd" if os.name == "nt" else "npm"
    resolved = shutil.which(command)
    if resolved is None:
        raise RuntimeError(f"{command} is required for Extension Host smoke")
    return resolved


# 校验 Extension Host JSON 与真实审批 PNG 的身份、能力和内容哈希
def _validate_evidence(
    evidence: Path,
    *,
    expected_commit: str,
    screenshot: Path | None,
) -> None:
    try:
        payload = json.loads(evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Extension Host produced invalid JSON evidence") from exc
    if not isinstance(payload, dict) or payload.get("status") != "passed":
        raise RuntimeError("Extension Host evidence is not passed")
    if payload.get("commit") != expected_commit:
        raise RuntimeError("Extension Host evidence commit mismatch")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise RuntimeError("Extension Host evidence capabilities are missing")
    required = {"activation", "create_thread", "resume_thread", "open_diff"}
    if any(capabilities.get(name) is not True for name in required):
        raise RuntimeError("Extension Host evidence is missing a required capability")
    if screenshot is None:
        return
    if capabilities.get("approval_visual") is not True:
        raise RuntimeError("Extension Host approval visual capability is missing")
    try:
        image = screenshot.read_bytes()
    except OSError as exc:
        raise RuntimeError("Extension Host approval screenshot is missing") from exc
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("Extension Host approval screenshot is not a PNG")
    screenshot_evidence = payload.get("approval_screenshot")
    if not isinstance(screenshot_evidence, dict):
        raise RuntimeError("Extension Host screenshot evidence is missing")
    digest = hashlib.sha256(image).hexdigest()
    if screenshot_evidence.get("sha256") != digest:
        raise RuntimeError("Extension Host approval screenshot hash mismatch")
    if screenshot_evidence.get("bytes") != len(image):
        raise RuntimeError("Extension Host approval screenshot size mismatch")


# 执行真实 Extension Host smoke 并确保 daemon 被回收
def smoke(*, headless: bool, evidence: Path, screenshot: Path) -> None:
    evidence = evidence.resolve()
    screenshot = screenshot.resolve()
    evidence.parent.mkdir(parents=True, exist_ok=True)
    capture_approval = headless and platform.system() == "Linux"
    approval_path = screenshot if capture_approval else None
    evidence.unlink(missing_ok=True)
    if approval_path is not None:
        approval_path.parent.mkdir(parents=True, exist_ok=True)
        approval_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="coderook-vscode-host-",
        ignore_cleanup_errors=True,
    ) as raw_temp:
        home = Path(raw_temp) / "home"
        home.mkdir()
        env = _extension_environment(home, evidence, approval_path)
        daemon = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from code_rook.core.app import run; run()",
            ],
            cwd=_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            asyncio.run(
                _wait_until_listening(int(env["CODEROOK_PORT"]), daemon)
            )
            command = [_npm_command(), "run", "test:host"]
            if headless and platform.system() == "Linux":
                xvfb = shutil.which("xvfb-run")
                if xvfb is None:
                    raise RuntimeError("xvfb-run is required for headless Linux smoke")
                command = [xvfb, "-a", *command]
            subprocess.run(
                command,
                cwd=_EXTENSION,
                env=env,
                check=True,
                timeout=180,
            )
            if not evidence.is_file():
                raise RuntimeError("Extension Host did not produce evidence")
            _validate_evidence(
                evidence,
                expected_commit=env["CODEROOK_VSCODE_TEST_COMMIT"],
                screenshot=approval_path,
            )
        finally:
            if daemon.poll() is None:
                daemon.terminate()
                try:
                    daemon.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    daemon.kill()
                    daemon.wait(timeout=5)


# 运行 Extension Host smoke 命令
def main() -> int:
    args = _parse_args()
    smoke(
        headless=args.headless,
        evidence=args.evidence,
        screenshot=args.screenshot,
    )
    print(f"VS Code Extension Host smoke passed: {args.evidence.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

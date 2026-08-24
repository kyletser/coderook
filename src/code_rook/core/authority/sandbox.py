from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from code_rook.core.authority.models import SandboxCapability

SandboxProbe = Callable[[list[str]], tuple[bool, str]]


# 在有界子进程中实际启动沙箱包装器，验证内核策略而不只检查可执行文件存在
def _run_sandbox_probe(argv: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    if completed.returncode != 0:
        return False, f"exit code {completed.returncode}"
    return True, "probe succeeded"


# 用生产策略构建并执行隔离探针，验证工作区可读且宿主 Home 与凭据哨兵不可读
def _probe_sandbox_policy(
    executable: str,
    *,
    kind: str,
    run_probe: SandboxProbe,
) -> tuple[bool, str]:
    from code_rook.core.sandbox.planner import (
        build_bwrap_argv,
        build_seatbelt_profile,
    )

    try:
        with (
            tempfile.TemporaryDirectory(prefix="coderook-sandbox-workspace-") as workspace_raw,
            tempfile.TemporaryDirectory(
                prefix=".coderook-sandbox-private-",
                dir=Path.home(),
            ) as private_raw,
        ):
            workspace = Path(workspace_raw).resolve()
            private_home = Path(private_raw).resolve()
            workspace_marker = workspace / "workspace-readable.txt"
            home_marker = private_home / "home-secret.txt"
            credential_marker = private_home / ".ssh" / "id_probe"
            workspace_marker.write_text("workspace-ok", encoding="utf-8")
            home_marker.write_text("must-not-read", encoding="utf-8")
            credential_marker.parent.mkdir(parents=True)
            credential_marker.write_text("must-not-read", encoding="utf-8")
            command = " && ".join(
                (
                    f"cd {shlex.quote(str(workspace))}",
                    f"test -r {shlex.quote(str(workspace_marker))}",
                    f"test ! -r {shlex.quote(str(home_marker))}",
                    f"test ! -r {shlex.quote(str(credential_marker))}",
                )
            )
            if kind == "linux_bwrap":
                argv = build_bwrap_argv(
                    str(workspace),
                    writable=False,
                    network=False,
                )
                argv[0] = executable
                argv += ["--chdir", str(workspace), "--", "/bin/sh", "-c", command]
            else:
                profile = build_seatbelt_profile(
                    str(workspace),
                    writable=False,
                    network=False,
                )
                argv = [executable, "-p", profile, "/bin/sh", "-c", command]
            return run_probe(argv)
    except (OSError, RuntimeError) as exc:
        return False, f"probe setup failed ({type(exc).__name__})"


# 探测当前平台的 OS 隔离后端；可用时由 Bash 执行路径实际包裹命令，不可用时强制降级审批
def detect_sandbox_capability(
    *,
    platform: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
    run_probe: SandboxProbe = _run_sandbox_probe,
) -> SandboxCapability:
    target = platform or sys.platform
    if target == "win32":
        return SandboxCapability(
            available=False,
            kind="windows_none",
            reason="no OS isolation backend",
        )
    if target.startswith("linux"):
        executable = find_executable("bwrap")
        if executable is not None:
            probe_ok, probe_reason = _probe_sandbox_policy(
                executable,
                kind="linux_bwrap",
                run_probe=run_probe,
            )
            if not probe_ok:
                return SandboxCapability(
                    available=False,
                    kind="none",
                    reason=f"bubblewrap probe failed ({probe_reason})",
                )
            return SandboxCapability(
                available=True,
                kind="linux_bwrap",
                reason="bubblewrap execution probe succeeded",
            )
        return SandboxCapability(
            available=False,
            kind="none",
            reason="bubblewrap executable is unavailable",
        )
    if target == "darwin":
        executable = find_executable("sandbox-exec")
        if executable is not None:
            probe_ok, probe_reason = _probe_sandbox_policy(
                executable,
                kind="macos_seatbelt",
                run_probe=run_probe,
            )
            if not probe_ok:
                return SandboxCapability(
                    available=False,
                    kind="none",
                    reason=f"Seatbelt probe failed ({probe_reason})",
                )
            return SandboxCapability(
                available=True,
                kind="macos_seatbelt",
                reason="Seatbelt execution probe succeeded",
            )
        return SandboxCapability(
            available=False,
            kind="none",
            reason="Seatbelt sandbox-exec is unavailable",
        )
    return SandboxCapability(
        available=False,
        kind="none",
        reason=f"unsupported platform: {target}",
    )

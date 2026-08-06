from __future__ import annotations

import shutil
import sys
from collections.abc import Callable

from code_rook.core.authority.models import SandboxCapability


# 探测当前平台的 OS 隔离后端可用性（advisory）：结果仅作为权限快照与回执中的
# 元数据呈现，CodeRook 目前不会用它实际包裹任何子进程，强制防线是权限审批链
def detect_sandbox_capability(
    *,
    platform: str | None = None,
    find_executable: Callable[[str], str | None] = shutil.which,
) -> SandboxCapability:
    target = platform or sys.platform
    if target == "win32":
        return SandboxCapability(
            available=False,
            kind="windows_none",
            reason="no OS isolation backend",
        )
    if target.startswith("linux"):
        if find_executable("bwrap") is not None:
            return SandboxCapability(
                available=True,
                kind="linux_bwrap",
                reason="bubblewrap executable is available",
            )
        return SandboxCapability(
            available=False,
            kind="none",
            reason="bubblewrap executable is unavailable",
        )
    if target == "darwin":
        if find_executable("sandbox-exec") is not None:
            return SandboxCapability(
                available=True,
                kind="macos_seatbelt",
                reason="Seatbelt sandbox-exec is available",
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

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from code_rook.core.authority.models import SandboxCapability


class SandboxTier(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    NONE = "none"


@dataclass(frozen=True)
class SandboxPlan:
    # 计划应用的沙箱档位；NONE 表示无真实隔离
    tier: SandboxTier
    capability: SandboxCapability
    # 诚实标注：本平台无可用后端时降级为"原样执行 + 仅审计"
    degraded: bool
    # 前置包装 argv（bwrap 参数，或 ["sandbox-exec", "-p", profile]）；degraded 时为空
    wrapper: list[str]
    reason: str

    @property
    def executable(self) -> str:
        # 包装器可执行文件；degraded 时返回空（直接执行原命令）
        return self.wrapper[0] if self.wrapper else ""


# 依据平台能力与目标档位构建沙箱执行计划；不可用后端一律降级并诚实标注
def plan_sandbox(
    capability: SandboxCapability,
    tier: SandboxTier,
    workspace: str,
) -> SandboxPlan:
    if tier == SandboxTier.NONE or not capability.available:
        return SandboxPlan(
            tier=SandboxTier.NONE,
            capability=capability,
            degraded=True,
            wrapper=[],
            reason=(
                capability.reason
                if not capability.available
                else "sandbox disabled by requested tier"
            ),
        )
    if capability.kind == "linux_bwrap":
        writable = tier == SandboxTier.WORKSPACE_WRITE
        wrapper = build_bwrap_argv(workspace, writable=writable) + _shell_trailer()
        return SandboxPlan(
            tier=tier,
            capability=capability,
            degraded=False,
            wrapper=wrapper,
            reason=f"bwrap {tier.value} wrap",
        )
    if capability.kind == "macos_seatbelt":
        writable = tier == SandboxTier.WORKSPACE_WRITE
        wrapper = ["sandbox-exec", "-p", build_seatbelt_profile(workspace, writable=writable)]
        wrapper += _shell_trailer()
        return SandboxPlan(
            tier=tier,
            capability=capability,
            degraded=False,
            wrapper=wrapper,
            reason=f"seatbelt {tier.value} wrap",
        )
    return SandboxPlan(
        tier=SandboxTier.NONE,
        capability=capability,
        degraded=True,
        wrapper=[],
        reason=f"no isolation backend for {capability.kind}",
    )


# 构建 bwrap argv：整机只读覆盖，workspace_write 时仅工作区与临时目录可写
def build_bwrap_argv(workspace: str, *, writable: bool) -> list[str]:
    ws = str(Path(workspace).resolve())
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--ro-bind", "/", "/",
    ]
    if writable:
        argv += ["--bind", ws, ws, "--tmpfs", "/tmp"]
    else:
        argv += ["--ro-bind", ws, ws]
    return argv


# 构建 macOS Seatbelt 沙箱 profile 文本；writable 时允许写工作区
def build_seatbelt_profile(workspace: str, *, writable: bool) -> str:
    ws = str(Path(workspace).resolve())
    write_rules = (
        f'(allow file-write* (subpath "{ws}"))\n    '
        '(allow file-write* (subpath "/tmp"))'
        if writable
        else ""
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        f'(allow file-read* (subpath "{ws}"))\n'
        '(allow file-read* (subpath "/usr"))\n'
        "(allow process*)\n"
        "(allow sysctl-read*)\n"
        + (f"    {write_rules}\n" if write_rules else "")
    )


# 返回把命令交给宿主 POSIX shell 执行的包装尾部（bwrap 需 -- 终止选项解析）
def _shell_trailer() -> list[str]:
    return ["--", "/bin/sh", "-c"]


# 把 shell 命令改写成"沙箱包装器 + sh -c <command>"的整条 shell 字符串；无包装器时原样返回
def wrap_sandbox_command(plan: SandboxPlan, command: str) -> str:
    if not plan.wrapper or plan.degraded:
        return command
    return shlex.join([*plan.wrapper, command])


# AUTO_REVIEW 姿态下 shell 命令可落到的最小档位；无沙箱时返回 NONE 使决策回落 ASK
def tier_for_auto_review(capability: SandboxCapability) -> SandboxTier:
    if not capability.available:
        return SandboxTier.NONE
    return SandboxTier.WORKSPACE_WRITE
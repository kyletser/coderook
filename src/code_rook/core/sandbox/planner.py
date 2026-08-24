from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from code_rook.core.authority.models import SandboxCapability

if TYPE_CHECKING:
    from code_rook.core.processes import ProcessSupervisor


class SandboxTier(StrEnum):
    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    NONE = "none"


class SandboxPolicyError(ValueError):
    """沙箱无法强制执行请求策略时的拒绝错误。"""


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
    workspace: str = ""
    network: bool = False
    allowed_domains: tuple[str, ...] = ()
    domain_policy_enforced: bool = False
    writable_roots: tuple[str, ...] = ()
    policy_version: int = 2

    @property
    def executable(self) -> str:
        # 包装器可执行文件；degraded 时返回空（直接执行原命令）
        return self.wrapper[0] if self.wrapper else ""

    @property
    # 只有存在真实包装器且未降级时才允许权限层视为强制隔离
    def enforced(self) -> bool:
        return bool(self.wrapper) and not self.degraded and self.tier != SandboxTier.NONE

    @property
    # 返回实际执行该计划的后端名称，降级时明确标为 degraded
    def backend(self) -> str:
        return self.capability.kind if self.enforced else "degraded"

    # 返回可写入 receipt 的完整隔离决策，不包含命令或环境变量
    def describe(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "tier": self.tier.value,
            "workspace": self.workspace,
            "network": self.network,
            "allowed_domains": list(self.allowed_domains),
            "domain_policy_enforced": self.domain_policy_enforced,
            "writable_roots": list(self.writable_roots),
            "enforced": self.enforced,
            "degraded_reason": self.reason if self.degraded else "",
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class SandboxSpawnRequest:
    label: str
    command: str | None = None
    argv: tuple[str, ...] = ()
    cwd: Path | None = None
    interactive_stdin: bool = False
    env: dict[str, str] | None = None
    stdin: Any = None
    stdout: Any = None
    stderr: Any = None

    # 保证调用方只能选择 shell command 或可信 argv 其中一种执行形式
    def __post_init__(self) -> None:
        if (self.command is None) == (not self.argv):
            raise ValueError("sandbox spawn requires exactly one of command or argv")


class SandboxBackend(Protocol):
    # 返回该后端启动时冻结的能力探测结果
    def probe(self) -> SandboxCapability: ...

    # 依据档位和工作区生成不可变执行计划
    def plan(
        self,
        tier: SandboxTier,
        workspace: str,
        *,
        network: bool = False,
        allowed_domains: tuple[str, ...] = (),
    ) -> SandboxPlan: ...

    # 通过统一进程监督边界启动一次性 shell
    async def spawn(
        self,
        plan: SandboxPlan,
        request: SandboxSpawnRequest,
        supervisor: ProcessSupervisor | None,
    ) -> asyncio.subprocess.Process: ...

    # 返回不含命令与凭据的后端决策说明
    def describe(self, plan: SandboxPlan) -> dict[str, object]: ...


class _BaseBackend:
    # 保存不可变能力快照，避免 plan 与 spawn 之间重新探测导致边界漂移
    def __init__(self, capability: SandboxCapability) -> None:
        self._capability = capability

    # 返回构造后端时冻结的能力快照
    def probe(self) -> SandboxCapability:
        return self._capability

    # 通过统一 supervisor 启动已包装命令，无 supervisor 时保留测试兼容路径
    async def spawn(
        self,
        plan: SandboxPlan,
        request: SandboxSpawnRequest,
        supervisor: ProcessSupervisor | None,
    ) -> asyncio.subprocess.Process:
        from code_rook.core.processes import (
            create_shell_process,
            sanitized_shell_environment,
        )

        process_env = sanitized_shell_environment(request.env)
        if plan.enforced and plan.capability.kind in {
            "linux_bwrap",
            "macos_seatbelt",
        }:
            process_env["HOME"] = "/tmp/coderook-home"

        if request.argv:
            argv = (
                (*plan.wrapper, shlex.join(request.argv))
                if plan.enforced
                else request.argv
            )
            if supervisor is not None:
                return await supervisor.start_exec(
                    *argv,
                    label=request.label,
                    cwd=request.cwd,
                    env=process_env,
                    stdin=request.stdin,
                    stdout=request.stdout,
                    stderr=request.stderr,
                )
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=request.cwd,
                env=process_env,
                stdin=request.stdin,
                stdout=request.stdout,
                stderr=request.stderr,
            )
        assert request.command is not None
        command = request.command
        command = wrap_sandbox_command(plan, command)
        if supervisor is not None:
            return await supervisor.start_shell(
                command,
                label=request.label,
                cwd=request.cwd,
                interactive_stdin=request.interactive_stdin,
                env=process_env,
            )
        return await create_shell_process(
            command,
            request.cwd,
            interactive_stdin=request.interactive_stdin,
            env=process_env,
        )

    # 返回计划自身的稳定说明，供审计和 receipt 持久化
    def describe(self, plan: SandboxPlan) -> dict[str, object]:
        return plan.describe()


class BwrapBackend(_BaseBackend):
    # 生成 Linux bubblewrap 隔离计划
    def plan(
        self,
        tier: SandboxTier,
        workspace: str,
        *,
        network: bool = False,
        allowed_domains: tuple[str, ...] = (),
    ) -> SandboxPlan:
        _reject_unenforceable_domain_policy(allowed_domains)
        resolved_workspace = str(Path(workspace).resolve())
        writable = tier == SandboxTier.WORKSPACE_WRITE
        wrapper = build_bwrap_argv(
            workspace,
            writable=writable,
            network=network,
        ) + _shell_trailer()
        return SandboxPlan(
            tier=tier,
            capability=self.probe(),
            degraded=False,
            wrapper=wrapper,
            reason=f"bwrap {tier.value} wrap",
            workspace=resolved_workspace,
            network=network,
            allowed_domains=allowed_domains,
            writable_roots=(resolved_workspace, "/tmp") if writable else (),
        )


class SeatbeltBackend(_BaseBackend):
    # 生成 macOS Seatbelt 隔离计划
    def plan(
        self,
        tier: SandboxTier,
        workspace: str,
        *,
        network: bool = False,
        allowed_domains: tuple[str, ...] = (),
    ) -> SandboxPlan:
        _reject_unenforceable_domain_policy(allowed_domains)
        resolved_workspace = str(Path(workspace).resolve())
        writable = tier == SandboxTier.WORKSPACE_WRITE
        wrapper = [
            "sandbox-exec",
            "-p",
            build_seatbelt_profile(workspace, writable=writable, network=network),
            "/bin/sh",
            "-c",
        ]
        return SandboxPlan(
            tier=tier,
            capability=self.probe(),
            degraded=False,
            wrapper=wrapper,
            reason=f"seatbelt {tier.value} wrap",
            workspace=resolved_workspace,
            network=network,
            allowed_domains=allowed_domains,
            writable_roots=(resolved_workspace, "/tmp") if writable else (),
        )


class DegradedBackend(_BaseBackend):
    # 生成明确不具备强制力的降级计划
    def plan(
        self,
        tier: SandboxTier,
        workspace: str,
        *,
        network: bool = False,
        allowed_domains: tuple[str, ...] = (),
    ) -> SandboxPlan:
        _reject_unenforceable_domain_policy(allowed_domains)
        capability = self.probe()
        reason = (
            capability.reason
            if not capability.available
            else (
                "sandbox disabled by requested tier"
                if tier == SandboxTier.NONE
                else f"no isolation backend for {capability.kind}"
            )
        )
        return SandboxPlan(
            tier=SandboxTier.NONE,
            capability=capability,
            degraded=True,
            wrapper=[],
            reason=reason,
            workspace=str(Path(workspace).resolve()),
            network=network,
            allowed_domains=allowed_domains,
        )


# 将冻结的能力快照映射为唯一后端实现，未知或禁用能力一律降级
def backend_for_capability(capability: SandboxCapability) -> SandboxBackend:
    if capability.available and capability.kind == "linux_bwrap":
        return BwrapBackend(capability)
    if capability.available and capability.kind == "macos_seatbelt":
        return SeatbeltBackend(capability)
    return DegradedBackend(capability)


# 依据平台能力与目标档位构建沙箱执行计划；不可用后端一律降级并诚实标注
def plan_sandbox(
    capability: SandboxCapability,
    tier: SandboxTier,
    workspace: str,
    *,
    network: bool = False,
    allowed_domains: tuple[str, ...] = (),
) -> SandboxPlan:
    backend = backend_for_capability(capability)
    if tier == SandboxTier.NONE:
        return DegradedBackend(capability).plan(
            tier,
            workspace,
            network=network,
            allowed_domains=allowed_domains,
        )
    return backend.plan(
        tier,
        workspace,
        network=network,
        allowed_domains=allowed_domains,
    )


# 对当前无法由 OS 沙箱强制执行的域名白名单请求立即拒绝，禁止退化为全网访问
def _reject_unenforceable_domain_policy(allowed_domains: tuple[str, ...]) -> None:
    if allowed_domains:
        domains = ", ".join(sorted(set(allowed_domains)))
        raise SandboxPolicyError(
            "domain allow-list enforcement is unavailable for shell sandboxes; "
            f"refusing unrestricted network access for: {domains}"
        )


# 通过计划绑定的后端统一启动 shell，调用方不再自行拼接 wrapper
async def spawn_sandboxed_shell(
    plan: SandboxPlan | None,
    request: SandboxSpawnRequest,
    supervisor: ProcessSupervisor | None,
) -> asyncio.subprocess.Process:
    capability = (
        plan.capability
        if plan is not None
        else SandboxCapability(available=False, kind="none", reason="no sandbox plan")
    )
    effective_plan = plan or DegradedBackend(capability).plan(
        SandboxTier.NONE,
        str(request.cwd or Path.cwd()),
    )
    backend = backend_for_capability(effective_plan.capability)
    return await backend.spawn(effective_plan, request, supervisor)


# 返回 bwrap 内允许只读挂载的系统运行时路径，避免暴露整个 /etc
def _bwrap_system_roots() -> list[str]:
    candidates = [
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc/ld.so.cache",
        "/etc/ld.so.conf",
        "/etc/ld.so.conf.d",
        "/etc/ssl",
        "/etc/ca-certificates",
        "/etc/pki",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/localtime",
        "/etc/timezone",
    ]
    return [path for path in candidates if Path(path).exists()]


# 返回 bwrap 创建工作区挂载点前所需的空父目录
def _bwrap_workspace_parents(workspace: str) -> list[str]:
    resolved = Path(workspace).resolve()
    parents = list(reversed(resolved.parents))
    return [str(path) for path in parents if str(path) not in {"/", "."}]


# 构建 bwrap argv：仅挂载系统运行时与工作区，宿主 Home 和根文件系统不可见
def build_bwrap_argv(
    workspace: str,
    *,
    writable: bool,
    network: bool = False,
) -> list[str]:
    ws = str(Path(workspace).resolve())
    argv = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/tmp/coderook-home",
        "--dir", "/etc",
        "--setenv", "HOME", "/tmp/coderook-home",
    ]
    for system_root in _bwrap_system_roots():
        argv += ["--ro-bind", system_root, system_root]
    for parent in _bwrap_workspace_parents(ws):
        argv += ["--dir", parent]
    if network:
        argv += ["--share-net"]
    if writable:
        argv += ["--bind", ws, ws]
    else:
        argv += ["--ro-bind", ws, ws]
    return argv


# 构建 macOS Seatbelt 沙箱 profile 文本；仅读取系统运行时、工作区和临时目录
def build_seatbelt_profile(
    workspace: str,
    *,
    writable: bool,
    network: bool = False,
) -> str:
    ws = str(Path(workspace).resolve()).replace("\\", "\\\\").replace('"', '\\"')
    write_rules = (
        f'(allow file-write* (subpath "{ws}"))\n    '
        '(allow file-write* (subpath "/tmp"))'
        if writable
        else ""
    )
    network_rules = "(allow network*)" if network else ""
    read_rules = (
        '(allow file-read* (subpath "/System") (subpath "/usr") '
        '(subpath "/bin") (subpath "/sbin") '
        '(literal "/private/etc/hosts") (literal "/private/etc/resolv.conf") '
        '(literal "/private/etc/passwd") (literal "/private/etc/group") '
        '(subpath "/private/etc/ssl") (subpath "/private/tmp") '
        f'(subpath "{ws}"))'
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        f"{read_rules}\n"
        "(allow process*)\n"
        "(allow sysctl-read)\n"
        + (f"    {write_rules}\n" if write_rules else "")
        + (f"    {network_rules}\n" if network_rules else "")
    )


# 返回把命令交给宿主 POSIX shell 执行的包装尾部（bwrap 需 -- 终止选项解析）
def _shell_trailer() -> list[str]:
    return ["--", "/bin/sh", "-c"]


# 把 shell 命令改写成"沙箱包装器 + sh -c <command>"的整条 shell 字符串；无包装器时原样返回
def wrap_sandbox_command(plan: SandboxPlan, command: str) -> str:
    if not plan.wrapper or plan.degraded:
        return command
    return shlex.join([*plan.wrapper, command])


# 返回适合常驻 shell 的沙箱 argv，移除一次性 sh -c 的命令参数约束
def persistent_sandbox_argv(plan: SandboxPlan) -> list[str] | None:
    if not plan.enforced:
        return None
    if len(plan.wrapper) >= 2 and plan.wrapper[-2:] == ["/bin/sh", "-c"]:
        return [*plan.wrapper[:-1]]
    return [*plan.wrapper]


# AUTO_REVIEW 姿态下 shell 命令可落到的最小档位；无沙箱时返回 NONE 使决策回落 ASK
def tier_for_auto_review(capability: SandboxCapability) -> SandboxTier:
    if not capability.available or capability.kind not in {
        "linux_bwrap",
        "macos_seatbelt",
    }:
        return SandboxTier.NONE
    return SandboxTier.WORKSPACE_WRITE

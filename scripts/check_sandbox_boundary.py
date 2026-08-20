from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from code_rook.core.authority.sandbox import detect_sandbox_capability
from code_rook.core.sandbox.planner import (
    SandboxPlan,
    SandboxPolicyError,
    SandboxTier,
    plan_sandbox,
    tier_for_auto_review,
    wrap_sandbox_command,
)


# 删除边界探针生成的文件，清理失败不遮蔽原始门禁结论
def _cleanup(*paths: Path) -> None:
    for path in paths:
        try:
            if path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


# 在真实计划中运行 shell 命令并捕获退出码和诊断
def _run(plan: SandboxPlan, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        wrap_sandbox_command(plan, command),
        shell=True,
        check=False,
        capture_output=True,
        text=True,
    )


# 断言真实沙箱拒绝命令且没有在目标路径留下文件
def _expect_denied(plan: SandboxPlan, command: str, target: Path, label: str) -> None:
    result = _run(plan, command)
    if result.returncode == 0 or target.exists():
        raise RuntimeError(f"sandbox allowed {label}")


# 在真实后端中执行写边界探针，验证工作区内可写且外部与只读工作区不可写
def _check_enforced_boundary(workspace: Path) -> None:
    marker = uuid.uuid4().hex
    probe_dir = workspace / ".coderook" / "sandbox-probe" / marker
    inside = probe_dir / "inside.txt"
    outside_root = Path.home().resolve()
    try:
        outside_root.relative_to(workspace)
    except ValueError:
        pass
    else:
        outside_root = workspace.parent
    outside = outside_root / f".coderook-outside-{marker}.txt"
    credential_root = Path.home().resolve() / ".coderook"
    credential = credential_root / f"credential-probe-{marker}.txt"
    system = Path("/etc") / f"coderook-system-probe-{marker}.txt"
    symlink = probe_dir / "outside-link"
    probe_dir.mkdir(parents=True, exist_ok=False)
    capability = detect_sandbox_capability()
    writable = plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, str(workspace))
    readonly = plan_sandbox(capability, SandboxTier.READ_ONLY, str(workspace))
    network_allowed = plan_sandbox(
        capability,
        SandboxTier.READ_ONLY,
        str(workspace),
        network=True,
    )
    if not writable.enforced or not readonly.enforced:
        raise RuntimeError("sandbox probe reported available backend but produced degraded plan")
    try:
        inside_command = f"printf inside > {shlex.quote(inside.as_posix())}"
        inside_result = _run(writable, inside_command)
        if inside_result.returncode != 0 or inside.read_text(encoding="utf-8") != "inside":
            raise RuntimeError(f"sandbox rejected workspace write: {inside_result.stderr}")

        outside_command = f"printf outside > {shlex.quote(outside.as_posix())}"
        _expect_denied(writable, outside_command, outside, "write outside workspace")

        if credential_root.is_dir():
            credential_command = f"printf secret > {shlex.quote(credential.as_posix())}"
            _expect_denied(
                writable,
                credential_command,
                credential,
                "write to credential directory",
            )

        system_command = f"printf system > {shlex.quote(system.as_posix())}"
        _expect_denied(writable, system_command, system, "write to system directory")

        symlink.symlink_to(outside)
        symlink_command = f"printf link > {shlex.quote(symlink.as_posix())}"
        _expect_denied(writable, symlink_command, outside, "symlink boundary escape")

        child_command = (
            f"(sleep 0.05; printf child > {shlex.quote(outside.as_posix())}) & "
            'child_pid=$!; wait "$child_pid"'
        )
        _expect_denied(writable, child_command, outside, "child-process escape")

        readonly_command = f"printf readonly > {shlex.quote(inside.as_posix())}"
        readonly_result = _run(readonly, readonly_command)
        if readonly_result.returncode == 0:
            raise RuntimeError("read-only sandbox allowed workspace write")

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(2)
            port = int(listener.getsockname()[1])
            connect_script = (
                "import socket;"
                f"socket.create_connection(('127.0.0.1',{port}),1).close()"
            )
            network_command = (
                f"{shlex.quote(sys.executable)} -c {shlex.quote(connect_script)}"
            )
            denied_network = _run(readonly, network_command)
            if denied_network.returncode == 0:
                raise RuntimeError("network-denied sandbox reached host listener")
            allowed_network = _run(network_allowed, network_command)
            if allowed_network.returncode != 0:
                raise RuntimeError(
                    "network-enabled sandbox could not reach host listener: "
                    f"{allowed_network.stderr}"
                )
    finally:
        _cleanup(symlink, probe_dir, outside, credential, system)


# 执行跨平台沙箱门禁；无真实后端时只接受明确降级且禁止 AUTO_REVIEW
def main() -> int:
    capability = detect_sandbox_capability()
    try:
        plan_sandbox(
            capability,
            SandboxTier.WORKSPACE_WRITE,
            str(Path.cwd()),
            network=True,
            allowed_domains=("api.example.com",),
        )
    except SandboxPolicyError:
        pass
    else:
        print("sandbox domain policy silently broadened network access", file=sys.stderr)
        return 1
    if not capability.available:
        plan = plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, str(Path.cwd()))
        if plan.enforced or not plan.degraded or tier_for_auto_review(capability) != SandboxTier.NONE:
            print("sandbox degraded-state contract failed", file=sys.stderr)
            return 1
        print(f"sandbox boundary: DEGRADED ({capability.kind}: {capability.reason})")
        return 0
    try:
        _check_enforced_boundary(Path.cwd().resolve())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"sandbox boundary: FAIL ({exc})", file=sys.stderr)
        return 1
    print(f"sandbox boundary: PASS ({capability.kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

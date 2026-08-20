from __future__ import annotations

import argparse
import json
import shlex
import shutil
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
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


# 构造单项沙箱检查的机器可读结果
def _check_result(name: str, status: str, detail: str = "") -> dict[str, str]:
    result = {"name": name, "status": status}
    if detail:
        result["detail"] = detail
    return result


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


# 在真实后端中执行全部边界探针并返回逐项机器可读结果
def _check_enforced_boundary(workspace: Path) -> list[dict[str, str]]:
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
    created_credential_root = not credential_root.exists()
    if created_credential_root:
        credential_root.mkdir(parents=True)
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
    checks = [_check_result("enforced_plan", "passed")]
    try:
        inside_command = f"printf inside > {shlex.quote(inside.as_posix())}"
        inside_result = _run(writable, inside_command)
        if inside_result.returncode != 0 or inside.read_text(encoding="utf-8") != "inside":
            raise RuntimeError(f"sandbox rejected workspace write: {inside_result.stderr}")
        checks.append(_check_result("workspace_write", "passed"))

        outside_command = f"printf outside > {shlex.quote(outside.as_posix())}"
        _expect_denied(writable, outside_command, outside, "write outside workspace")
        checks.append(_check_result("outside_workspace_write_denied", "passed"))

        credential_command = f"printf secret > {shlex.quote(credential.as_posix())}"
        _expect_denied(
            writable,
            credential_command,
            credential,
            "write to credential directory",
        )
        checks.append(_check_result("credential_write_denied", "passed"))

        system_command = f"printf system > {shlex.quote(system.as_posix())}"
        _expect_denied(writable, system_command, system, "write to system directory")
        checks.append(_check_result("system_write_denied", "passed"))

        symlink.symlink_to(outside)
        symlink_command = f"printf link > {shlex.quote(symlink.as_posix())}"
        _expect_denied(writable, symlink_command, outside, "symlink boundary escape")
        checks.append(_check_result("symlink_escape_denied", "passed"))

        child_command = (
            f"(sleep 0.05; printf child > {shlex.quote(outside.as_posix())}) & "
            'child_pid=$!; wait "$child_pid"'
        )
        _expect_denied(writable, child_command, outside, "child-process escape")
        checks.append(_check_result("child_process_escape_denied", "passed"))

        readonly_command = f"printf readonly > {shlex.quote(inside.as_posix())}"
        readonly_result = _run(readonly, readonly_command)
        if readonly_result.returncode == 0:
            raise RuntimeError("read-only sandbox allowed workspace write")
        checks.append(_check_result("readonly_workspace_write_denied", "passed"))

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
            checks.append(_check_result("network_denied", "passed"))
            allowed_network = _run(network_allowed, network_command)
            if allowed_network.returncode != 0:
                raise RuntimeError(
                    "network-enabled sandbox could not reach host listener: "
                    f"{allowed_network.stderr}"
                )
            checks.append(_check_result("network_allowed", "passed"))
    finally:
        _cleanup(symlink, probe_dir, outside, credential, system)
        if created_credential_root:
            try:
                credential_root.rmdir()
            except OSError:
                pass
    return checks


# 解析沙箱门禁输出参数
def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify the OS sandbox boundary")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


# 写入绑定当前平台与后端决策的确定性 JSON 证据
def _write_report(
    output: Path | None,
    *,
    capability_kind: str,
    capability_reason: str,
    enforced: bool,
    degraded: bool,
    checks: list[dict[str, str]],
    gate_passed: bool,
    error: str | None = None,
) -> None:
    if output is None:
        return
    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": sys.platform,
        "backend": capability_kind,
        "backend_reason": capability_reason,
        "enforced": enforced,
        "degraded": degraded,
        "checks": checks,
        "gate_passed": gate_passed,
        "error": error,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# 执行跨平台沙箱门禁；无真实后端时只接受明确降级且禁止 AUTO_REVIEW
def main() -> int:
    args = _parse_args()
    capability = detect_sandbox_capability()
    checks: list[dict[str, str]] = []
    enforced = False
    degraded = not capability.available
    try:
        plan_sandbox(
            capability,
            SandboxTier.WORKSPACE_WRITE,
            str(Path.cwd()),
            network=True,
            allowed_domains=("api.example.com",),
        )
    except SandboxPolicyError:
        checks.append(_check_result("domain_allowlist_fail_closed", "passed"))
    else:
        error = "sandbox domain policy silently broadened network access"
        checks.append(_check_result("domain_allowlist_fail_closed", "failed", error))
        _write_report(
            args.output,
            capability_kind=capability.kind,
            capability_reason=capability.reason,
            enforced=enforced,
            degraded=degraded,
            checks=checks,
            gate_passed=False,
            error=error,
        )
        print(error, file=sys.stderr)
        return 1
    if not capability.available:
        plan = plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, str(Path.cwd()))
        enforced = plan.enforced
        degraded = plan.degraded
        if plan.enforced or not plan.degraded:
            error = "sandbox degraded-state contract failed"
            checks.append(_check_result("degraded_plan_explicit", "failed", error))
            _write_report(
                args.output,
                capability_kind=capability.kind,
                capability_reason=capability.reason,
                enforced=enforced,
                degraded=degraded,
                checks=checks,
                gate_passed=False,
                error=error,
            )
            print(error, file=sys.stderr)
            return 1
        checks.append(_check_result("degraded_plan_explicit", "passed"))
        if tier_for_auto_review(capability) != SandboxTier.NONE:
            error = "sandbox degraded-state AUTO_REVIEW contract failed"
            checks.append(_check_result("auto_review_disabled", "failed", error))
            _write_report(
                args.output,
                capability_kind=capability.kind,
                capability_reason=capability.reason,
                enforced=enforced,
                degraded=degraded,
                checks=checks,
                gate_passed=False,
                error=error,
            )
            print(error, file=sys.stderr)
            return 1
        checks.append(_check_result("auto_review_disabled", "passed"))
        _write_report(
            args.output,
            capability_kind=capability.kind,
            capability_reason=capability.reason,
            enforced=enforced,
            degraded=degraded,
            checks=checks,
            gate_passed=True,
        )
        print(f"sandbox boundary: DEGRADED ({capability.kind}: {capability.reason})")
        return 0
    plan = plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, str(Path.cwd()))
    enforced = plan.enforced
    degraded = plan.degraded
    try:
        checks.extend(_check_enforced_boundary(Path.cwd().resolve()))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        error = str(exc)
        checks.append(_check_result("boundary_matrix", "failed", error))
        _write_report(
            args.output,
            capability_kind=capability.kind,
            capability_reason=capability.reason,
            enforced=enforced,
            degraded=degraded,
            checks=checks,
            gate_passed=False,
            error=error,
        )
        print(f"sandbox boundary: FAIL ({error})", file=sys.stderr)
        return 1
    _write_report(
        args.output,
        capability_kind=capability.kind,
        capability_reason=capability.reason,
        enforced=enforced,
        degraded=degraded,
        checks=checks,
        gate_passed=True,
    )
    print(f"sandbox boundary: PASS ({capability.kind})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

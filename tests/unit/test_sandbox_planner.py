from code_rook.core.authority.models import SandboxCapability
from code_rook.core.sandbox.planner import (
    SandboxPlan,
    SandboxTier,
    build_bwrap_argv,
    plan_sandbox,
    tier_for_auto_review,
    wrap_sandbox_command,
)


# 功能：无可用后端时所有档位都降级并诚实标注 degraded，wrapper 为空
# 设计：用 windows_none / none 能力快照驱动，验证降级路径不抛错且 reason 透出
def test_plan_sandbox_degrades_without_backend() -> None:
    cap = SandboxCapability(available=False, kind="none", reason="no bwrap")
    plan = plan_sandbox(cap, SandboxTier.WORKSPACE_WRITE, "/ws")
    assert plan.degraded is True
    assert plan.tier == SandboxTier.NONE
    assert plan.wrapper == []
    assert "no bwrap" in plan.reason


# 功能：bwrap 可用时生成整机只读 + 工作区可写的包装 argv
# 设计：仅做结构断言（bwrap 前缀 + --ro-bind / /），Windows 上不实际运行 bwrap
def test_plan_sandbox_bwrap_workspace_write() -> None:
    cap = SandboxCapability(available=True, kind="linux_bwrap", reason="bwrap ok")
    plan = plan_sandbox(cap, SandboxTier.WORKSPACE_WRITE, "/proj")
    assert plan.degraded is False
    assert plan.wrapper[:3] == ["bwrap", "--die-with-parent", "--new-session"]
    assert "--bind" in plan.wrapper
    assert plan.wrapper[0] == "bwrap"


# 功能：read_only 档位下 bwrap 用 --ro-bind 工作区且不带可写 bind
# 设计：断言 readonly 包装不含 --bind/--tmpfs，防止越权写
def test_plan_sandbox_bwrap_read_only_no_write_bind() -> None:
    cap = SandboxCapability(available=True, kind="linux_bwrap", reason="bwrap ok")
    plan = plan_sandbox(cap, SandboxTier.READ_ONLY, "/proj")
    assert plan.degraded is False
    assert "--tmpfs" not in plan.wrapper
    assert "--bind" not in plan.wrapper
    assert "--ro-bind" in plan.wrapper


# 功能：seatbelt 可用时生成 sandbox-exec 前缀 + profile；写档受 toggle 控制
# 设计：验证 writable 时 profile 含工作区 file-write*，readonly 时不含
def test_plan_sandbox_seatbelt_profile_toggles_write() -> None:
    cap = SandboxCapability(available=True, kind="macos_seatbelt", reason="sb ok")
    rw = plan_sandbox(cap, SandboxTier.WORKSPACE_WRITE, "/proj")
    ro = plan_sandbox(cap, SandboxTier.READ_ONLY, "/proj")
    assert rw.wrapper[0] == "sandbox-exec"
    rw_profile = " ".join(rw.wrapper[2:])
    ro_profile = " ".join(ro.wrapper[2:])
    assert "file-write*" in rw_profile
    assert "file-write*" not in ro_profile


# 功能：AUTO_REVIEW 姿态在无沙箱时返回 NONE，使权限决策回落 ASK
# 设计：这是"沙箱失败/越界 → 回落审批"的关键判定，直接断言档位归零
def test_tier_for_auto_review_falls_back_without_sandbox() -> None:
    cap = SandboxCapability(available=False, kind="windows_none", reason="none")
    assert tier_for_auto_review(cap) == SandboxTier.NONE
    ok = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    assert tier_for_auto_review(ok) == SandboxTier.WORKSPACE_WRITE


# 功能：build_bwrap_argv 独立构造同名包装参数
# 设计：直接验证 argv 的只读覆盖与可写绑定，避免依赖 plan_sandbox 的状态
def test_build_bwrap_argv_structure() -> None:
    assert build_bwrap_argv("/proj", writable=False)[0] == "bwrap"
    writable = build_bwrap_argv("/proj", writable=True)
    assert "--tmpfs" in writable
    assert "/tmp" in writable


# 功能：真实沙箱计划把 shell 命令改写成 包装器 + sh -c，命令被整体引用防止注入
# 设计：验证 wrapped 以 bwrap 前缀开且以 -- /bin/sh -c 结尾，command 成为 sh -c 的单参数
def test_wrap_sandbox_command_builds_wrapper() -> None:
    cap = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    plan = plan_sandbox(cap, SandboxTier.WORKSPACE_WRITE, "/proj")
    wrapped = wrap_sandbox_command(plan, "uv run pytest")
    assert wrapped.startswith("bwrap ")
    assert wrapped.endswith("-- /bin/sh -c 'uv run pytest'")


# 功能：降级计划（无后端/read-only 不可用）原样返回命令，不施加任何包装
# 设计：degraded 计划 wrapper 为空，wrap 必须短路返回原名，避免在审计降级时误包
def test_wrap_sandbox_command_degraded_noop() -> None:
    cap = SandboxCapability(available=False, kind="windows_none", reason="none")
    plan = SandboxPlan(
        tier=SandboxTier.NONE,
        capability=cap,
        degraded=True,
        wrapper=[],
        reason="dec",
    )
    assert wrap_sandbox_command(plan, "echo hi") == "echo hi"
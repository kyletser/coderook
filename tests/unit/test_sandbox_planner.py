from code_rook.core.authority.models import SandboxCapability
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.sandbox.planner import (
    BwrapBackend,
    DegradedBackend,
    SandboxPlan,
    SandboxPolicyError,
    SandboxSpawnRequest,
    SandboxTier,
    SeatbeltBackend,
    backend_for_capability,
    build_bwrap_argv,
    persistent_sandbox_argv,
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
    assert plan.enforced is False
    assert plan.backend == "degraded"
    assert "no bwrap" in plan.reason


# 功能：bwrap 可用时生成整机只读 + 工作区可写的包装 argv
# 设计：仅做结构断言（bwrap 前缀 + --ro-bind / /），Windows 上不实际运行 bwrap
def test_plan_sandbox_bwrap_workspace_write() -> None:
    cap = SandboxCapability(available=True, kind="linux_bwrap", reason="bwrap ok")
    plan = plan_sandbox(cap, SandboxTier.WORKSPACE_WRITE, "/proj")
    assert plan.degraded is False
    assert plan.enforced is True
    assert plan.backend == "linux_bwrap"
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
    assert "(allow sysctl-read)" in rw_profile
    assert "sysctl-read*" not in rw_profile


# 功能：AUTO_REVIEW 姿态在无沙箱时返回 NONE，使权限决策回落 ASK
# 设计：这是"沙箱失败/越界 → 回落审批"的关键判定，直接断言档位归零
def test_tier_for_auto_review_falls_back_without_sandbox() -> None:
    cap = SandboxCapability(available=False, kind="windows_none", reason="none")
    assert tier_for_auto_review(cap) == SandboxTier.NONE
    ok = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    assert tier_for_auto_review(ok) == SandboxTier.WORKSPACE_WRITE


# 功能：验证不受支持但声称可用的后端不能触发 AUTO_REVIEW 自动放行
# 设计：构造 Windows capability 误报 available，断言档位回落 NONE，覆盖能力误报边界
def test_tier_for_auto_review_rejects_unsupported_available_backend() -> None:
    capability = SandboxCapability(available=True, kind="windows_none", reason="bad probe")

    assert tier_for_auto_review(capability) == SandboxTier.NONE
    assert plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, "/proj").enforced is False


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


# 功能：真实沙箱的常驻 shell argv 保留边界参数但移除一次性 -c
# 设计：从 bwrap 计划派生 argv，断言末尾是 /bin/sh 且后续命令只能经其 stdin 输入
def test_persistent_sandbox_argv_removes_one_shot_flag() -> None:
    capability = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    plan = plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, "/proj")

    argv = persistent_sandbox_argv(plan)

    assert argv is not None
    assert argv[-2:] == ["--", "/bin/sh"]
    assert argv[0] == "bwrap"


# 功能：验证 macOS Seatbelt 一次性与常驻 shell argv 都不包含不受支持的双横线分隔符
# 设计：用模拟能力检查明确 argv 顺序，并复用常驻转换覆盖移除 -c 的路径
def test_seatbelt_shell_argv_uses_native_command_position() -> None:
    capability = SandboxCapability(available=True, kind="macos_seatbelt", reason="ok")
    plan = plan_sandbox(capability, SandboxTier.WORKSPACE_WRITE, '/tmp/project "quoted"')

    assert plan.wrapper[-2:] == ["/bin/sh", "-c"]
    assert "--" not in plan.wrapper
    assert '\\"quoted\\"' in plan.wrapper[2]
    assert persistent_sandbox_argv(plan)[-1] == "/bin/sh"


# 功能：验证能力快照只会映射到对应的显式 SandboxBackend，未知后端强制降级
# 设计：分别构造 Linux、macOS 和 Windows 能力，直接检查实现类型以锁定 fail-closed 分派
def test_backend_factory_maps_only_enforced_capabilities() -> None:
    linux = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    macos = SandboxCapability(available=True, kind="macos_seatbelt", reason="ok")
    windows = SandboxCapability(available=True, kind="windows_none", reason="bad")

    assert isinstance(backend_for_capability(linux), BwrapBackend)
    assert isinstance(backend_for_capability(macos), SeatbeltBackend)
    assert isinstance(backend_for_capability(windows), DegradedBackend)


# 功能：验证 SandboxPlan 完整描述强制力、工作区、网络、可写根和策略版本
# 设计：从真实 planner 生成只读计划并检查稳定审计字典，不依赖 wrapper argv 的内部布局
def test_sandbox_plan_description_is_receipt_ready() -> None:
    capability = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    plan = plan_sandbox(capability, SandboxTier.READ_ONLY, "/proj")

    description = backend_for_capability(capability).describe(plan)

    assert description["backend"] == "linux_bwrap"
    assert description["tier"] == "read_only"
    assert description["network"] is False
    assert description["allowed_domains"] == []
    assert description["domain_policy_enforced"] is False
    assert description["writable_roots"] == []
    assert description["enforced"] is True
    assert description["policy_version"] == 2


# 功能：验证 network 策略显式改变 Linux 与 macOS wrapper 且进入可审计计划
# 设计：同一工作区分别生成联网计划，断言 bwrap 共享网络、Seatbelt 放行网络并记录 network=true
def test_network_policy_is_explicit_in_platform_wrappers() -> None:
    linux = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    macos = SandboxCapability(available=True, kind="macos_seatbelt", reason="ok")

    linux_plan = plan_sandbox(
        linux,
        SandboxTier.WORKSPACE_WRITE,
        "/proj",
        network=True,
    )
    macos_plan = plan_sandbox(
        macos,
        SandboxTier.WORKSPACE_WRITE,
        "/proj",
        network=True,
    )

    assert "--share-net" in linux_plan.wrapper
    assert linux_plan.network is True
    assert "(allow network*)" in macos_plan.wrapper[2]
    assert macos_plan.network is True


# 功能：验证域名白名单在后端不能真实强制时拒绝执行，不会退化为不受限联网
# 设计：覆盖 Linux、macOS 与降级后端的统一入口，断言全部在生成 wrapper 前 fail closed
def test_domain_allow_list_fails_closed_on_all_current_backends() -> None:
    capabilities = (
        SandboxCapability(available=True, kind="linux_bwrap", reason="ok"),
        SandboxCapability(available=True, kind="macos_seatbelt", reason="ok"),
        SandboxCapability(available=False, kind="windows_none", reason="none"),
    )

    for capability in capabilities:
        try:
            plan_sandbox(
                capability,
                SandboxTier.WORKSPACE_WRITE,
                "/proj",
                network=True,
                allowed_domains=("api.example.com",),
            )
        except SandboxPolicyError as exc:
            assert "refusing unrestricted network access" in str(exc)
        else:
            raise AssertionError("domain allow-list must fail closed")


# 功能：验证降级后端仍通过统一 spawn 边界执行，并由 ProcessSupervisor 登记回收
# 设计：运行跨平台 echo 真子进程而不 mock planner，覆盖 degraded 不伪装隔离但保留进程治理
async def test_degraded_backend_spawn_uses_process_supervisor() -> None:
    capability = SandboxCapability(available=False, kind="none", reason="unavailable")
    backend = backend_for_capability(capability)
    plan = backend.plan(SandboxTier.WORKSPACE_WRITE, ".")
    supervisor = ProcessSupervisor()

    process = await backend.spawn(
        plan,
        SandboxSpawnRequest(command="echo backend-spawn", label="sandbox-test"),
        supervisor,
    )
    stdout, _stderr = await process.communicate()
    records = supervisor.snapshot()
    supervisor.forget(process)

    assert b"backend-spawn" in stdout
    assert records[0].label == "sandbox-test"
    assert supervisor.snapshot() == ()

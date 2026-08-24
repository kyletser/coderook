from __future__ import annotations

from code_rook.core.authority import detect_sandbox_capability


# 功能：验证 Windows 明确报告没有可用 OS sandbox，而不是把审批机制冒充隔离
# 设计：注入 win32 平台避免依赖 CI 主机，精确检查 available、kind 和用户可见原因
def test_windows_reports_no_os_sandbox() -> None:
    capability = detect_sandbox_capability(platform="win32")

    assert capability.available is False
    assert capability.kind == "windows_none"
    assert capability.reason == "no OS isolation backend"


# 功能：验证 Linux 仅在真实找到 bubblewrap 可执行文件时报告 sandbox 可用
# 设计：注入可执行文件探测器覆盖存在与缺失两条路径，不依赖测试机器安装状态
def test_linux_sandbox_reflects_bubblewrap_detection() -> None:
    seen: list[list[str]] = []

    # 记录真实生产策略生成的探针参数并模拟内核接受
    def _success(argv: list[str]) -> tuple[bool, str]:
        seen.append(argv)
        return True, "ok"

    available = detect_sandbox_capability(
        platform="linux",
        find_executable=lambda name: f"/usr/bin/{name}",
        run_probe=_success,
    )
    unavailable = detect_sandbox_capability(
        platform="linux",
        find_executable=lambda _name: None,
        run_probe=lambda _argv: (True, "must not run"),
    )

    assert available.available is True
    assert available.kind == "linux_bwrap"
    assert "--tmpfs" in seen[0] and "/tmp" in seen[0]
    assert not any(
        seen[0][index : index + 3] == ["--ro-bind", "/", "/"] for index in range(len(seen[0]) - 2)
    )
    assert "--ro-bind" in seen[0]
    assert "--chdir" in seen[0]
    assert "workspace-readable.txt" in seen[0][-1]
    assert "id_probe" in seen[0][-1]
    assert "test -r" in seen[0][-1]
    assert "test ! -r" in seen[0][-1]
    assert unavailable.available is False
    assert unavailable.kind == "none"


# 功能：验证可执行文件存在但真实 bwrap 探针失败时立即降级
# 设计：分别注入查找成功和内核拒绝结果，断言不会把“文件存在”冒充强制隔离能力
def test_linux_sandbox_probe_failure_degrades_capability() -> None:
    capability = detect_sandbox_capability(
        platform="linux",
        find_executable=lambda _name: "/usr/bin/bwrap",
        run_probe=lambda _argv: (False, "exit code 1"),
    )

    assert capability.available is False
    assert capability.kind == "none"
    assert capability.reason == "bubblewrap probe failed (exit code 1)"


# 功能：验证 macOS Seatbelt 也必须通过真实执行探针后才报告可用
# 设计：注入成功探针并检查 argv，再用失败探针固定 fail-closed 分支
def test_macos_seatbelt_requires_execution_probe() -> None:
    seen: list[list[str]] = []

    # 记录探针 argv 并模拟系统成功执行
    def _success(argv: list[str]) -> tuple[bool, str]:
        seen.append(argv)
        return True, "ok"

    available = detect_sandbox_capability(
        platform="darwin",
        find_executable=lambda _name: "/usr/bin/sandbox-exec",
        run_probe=_success,
    )
    failed = detect_sandbox_capability(
        platform="darwin",
        find_executable=lambda _name: "/usr/bin/sandbox-exec",
        run_probe=lambda _argv: (False, "denied"),
    )

    assert available.available is True and available.kind == "macos_seatbelt"
    assert seen[0][0] == "/usr/bin/sandbox-exec"
    assert "(deny default)" in seen[0][2]
    assert "(allow default)" not in seen[0][2]
    assert "test -r" in seen[0][-1]
    assert "test ! -r" in seen[0][-1]
    assert failed.available is False and "probe failed" in failed.reason

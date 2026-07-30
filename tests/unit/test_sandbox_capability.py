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
    available = detect_sandbox_capability(
        platform="linux",
        find_executable=lambda name: f"/usr/bin/{name}",
    )
    unavailable = detect_sandbox_capability(
        platform="linux",
        find_executable=lambda _name: None,
    )

    assert available.available is True
    assert available.kind == "linux_bwrap"
    assert unavailable.available is False
    assert unavailable.kind == "none"

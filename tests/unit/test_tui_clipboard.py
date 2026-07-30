from __future__ import annotations

import subprocess

import pytest

from code_rook.tui import clipboard


# 功能：验证 Windows 剪贴板后端通过标准输入传递 Unicode 文本且不拼接到命令行
# 设计：替换平台、可执行文件发现和 subprocess.run，检查中文原文只出现在 input 参数
def test_windows_clipboard_uses_utf8_standard_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    # 记录子进程参数并模拟成功退出
    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(clipboard.os, "name", "nt")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _name: "pwsh.exe")
    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    assert clipboard.copy_to_windows_clipboard("中文 clipboard")
    assert calls[0]["input"] == "中文 clipboard"
    assert calls[0]["encoding"] == "utf-8"
    assert "中文 clipboard" not in calls[0]["args"]


# 功能：验证非 Windows 平台不会启动外部进程并明确返回未处理
# 设计：把平台设为 posix 并让 subprocess.run 一旦调用就失败，覆盖跨平台快速返回路径
def test_windows_clipboard_skips_other_platforms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clipboard.os, "name", "posix")
    monkeypatch.setattr(
        clipboard.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert not clipboard.copy_to_windows_clipboard("text")

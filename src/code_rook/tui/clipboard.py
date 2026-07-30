from __future__ import annotations

import os
import shutil
import subprocess

_POWERSHELL_COPY_COMMAND = (
    "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$value = [Console]::In.ReadToEnd(); Set-Clipboard -Value $value"
)


# 在 Windows 上通过系统 PowerShell 写入真实剪贴板，绕过终端对 OSC 52 的兼容差异
def copy_to_windows_clipboard(text: str) -> bool:
    if os.name != "nt" or not text:
        return False
    executable = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _POWERSHELL_COPY_COMMAND,
            ],
            input=text,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0

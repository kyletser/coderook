from __future__ import annotations

import io
import os
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageGrab

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


# 从系统剪贴板读取截图或已复制的图片文件，并统一编码为可持久化图片字节
def read_clipboard_image() -> bytes | None:
    try:
        clipboard = ImageGrab.grabclipboard()
    except OSError:
        return None
    if isinstance(clipboard, Image.Image):
        output = io.BytesIO()
        clipboard.save(output, format="PNG")
        return output.getvalue()
    if isinstance(clipboard, list):
        for value in clipboard:
            path = Path(value)
            if path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                try:
                    return path.read_bytes()
                except OSError:
                    continue
    return None

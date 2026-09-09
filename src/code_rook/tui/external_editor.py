from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path


class ExternalEditorError(RuntimeError):
    """外部编辑器无法启动或异常退出。"""


# 按平台拆分用户配置的编辑器命令并保留 Windows 路径中的反斜杠
def split_editor_command(command: str, *, windows: bool) -> list[str]:
    parts = shlex.split(command, posix=not windows)
    if windows:
        parts = [
            part[1:-1]
            if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}
            else part
            for part in parts
        ]
    if not parts:
        raise ExternalEditorError("external editor command is empty")
    return parts


# 从 VISUAL、EDITOR 或平台默认值构建外部编辑器启动命令
def resolve_editor_command(
    environment: Mapping[str, str] | None = None,
    *,
    windows: bool | None = None,
) -> list[str]:
    env = os.environ if environment is None else environment
    is_windows = os.name == "nt" if windows is None else windows
    configured = env.get("VISUAL") or env.get("EDITOR")
    command = configured or ("notepad.exe" if is_windows else "vi")
    return split_editor_command(command, windows=is_windows)


# 用临时 Markdown 文件打开外部编辑器并返回用户保存后的输入内容
def edit_text_externally(
    text: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    command = resolve_editor_command(environment)
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            prefix="coderook-prompt-",
            encoding="utf-8",
            newline="\n",
            delete=False,
        ) as handle:
            handle.write(text)
            path = Path(handle.name)
        completed = subprocess.run([*command, str(path)], check=False)
        if completed.returncode != 0:
            raise ExternalEditorError(
                f"external editor exited with code {completed.returncode}"
            )
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ExternalEditorError(str(exc)) from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)

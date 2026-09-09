from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from code_rook.tui.external_editor import (
    edit_text_externally,
    resolve_editor_command,
    split_editor_command,
)


# 功能：Windows 编辑器配置支持带空格可执行路径和额外等待参数
# 设计：直接固定命令拆分结果，防止 POSIX shlex 破坏 Windows 反斜杠路径
def test_split_editor_command_preserves_windows_path() -> None:
    assert split_editor_command('"C:\\Program Files\\Editor\\edit.exe" --wait', windows=True) == [
        "C:\\Program Files\\Editor\\edit.exe",
        "--wait",
    ]


# 功能：编辑器选择遵循 VISUAL 高于 EDITOR 并在 Windows 回退到记事本
# 设计：注入纯环境字典和显式平台值，不依赖开发机上的真实编辑器配置
def test_resolve_editor_command_priority_and_default() -> None:
    assert resolve_editor_command(
        {"VISUAL": "code --wait", "EDITOR": "vim"}, windows=True
    ) == ["code", "--wait"]
    assert resolve_editor_command({}, windows=True) == ["notepad.exe"]


# 功能：外部编辑器保存的文本会返回给 composer，临时文件随后被删除
# 设计：替换 subprocess.run 模拟编辑器原地保存，覆盖真实临时文件读写而不弹出 GUI
def test_edit_text_externally_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[Path] = []

    # 模拟外部编辑器读取初稿并写回用户修改后的内容
    def _fake_run(command: list[str], *, check: bool) -> subprocess.CompletedProcess[Any]:
        assert check is False
        path = Path(command[-1])
        assert path.read_text(encoding="utf-8") == "draft"
        path.write_text("edited\ntext", encoding="utf-8")
        captured.append(path)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("code_rook.tui.external_editor.subprocess.run", _fake_run)

    assert edit_text_externally("draft", environment={"EDITOR": "fake-editor"}) == (
        "edited\ntext"
    )
    assert captured and not captured[0].exists()

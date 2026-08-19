from __future__ import annotations

from pathlib import Path

from code_rook.tui.widgets.input import _pasted_image_path


# 功能：验证 TUI 只把单一真实图片路径识别为附件粘贴
# 设计：比较带引号 PNG 路径、普通文本和非图片文件，避免误吞用户正常粘贴内容
def test_pasted_image_path_recognizes_only_image_files(tmp_path: Path) -> None:
    image = tmp_path / "screen shot.png"
    image.write_bytes(b"png")
    text = tmp_path / "notes.txt"
    text.write_text("notes", encoding="utf-8")

    assert _pasted_image_path(f'"{image}"') == image.resolve()
    assert _pasted_image_path(str(text)) is None
    assert _pasted_image_path("ordinary pasted text") is None

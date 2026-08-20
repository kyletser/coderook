from pathlib import Path

from scripts.capture_tui_demo import capture_tui_demo


# 功能：验证 README TUI 演示由真实 Textual 应用离线生成且包含核心产品状态
# 设计：在临时路径运行确定性截图器，检查 SVG、计划、仓库上下文和验证闭环文本
async def test_capture_tui_demo_exports_real_product_state(tmp_path: Path) -> None:
    output = tmp_path / "tui-demo.svg"

    await capture_tui_demo(output)

    svg = output.read_text(encoding="utf-8")
    assert svg.startswith("<svg")
    assert "CodeRook&#160;TUI&#160;·&#160;verified&#160;coding&#160;task" in svg
    assert "Repository&#160;context" in svg
    assert "Verification&#160;passed" in svg
    assert "pytest" in svg

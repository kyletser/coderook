import base64
from io import BytesIO
from pathlib import Path

from PIL import Image

from code_rook.core.agent_runtime.images import normalize_tool_images, process_image
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.builtin.read_image import ReadImageTool
from code_rook.core.workspace import WorkspaceBoundary


# 功能：大图按 Pi 尺寸约束缩放并附带坐标映射，小图保持原始编码
# 设计：用内存生成确定性图片检查真实编码尺寸，不依赖外部文件或图像服务
def test_image_resize_dimensions_and_passthrough() -> None:
    stream = BytesIO()
    Image.new("RGB", (4000, 1000), "white").save(stream, format="PNG")
    converted = process_image(stream.getvalue(), "image/png")
    assert converted is not None
    data, mime, note = converted
    with Image.open(BytesIO(base64.b64decode(data))) as image:
        assert image.size == (2000, 500)
    assert mime == "image/png"
    assert "Multiply coordinates by 2.00" in note
    assert process_image(base64.b64decode(data), mime) == (data, mime, "")


# 功能：工具图片归一化不修改原结果且无法解码的图片保留，文字不丢失
# 设计：同一结果包含有效大图和未知格式，验证各块独立处理并检查二次归一化幂等
async def test_tool_image_normalization() -> None:
    stream = BytesIO()
    Image.new("RGB", (2400, 600), "blue").save(stream, format="PNG")
    original = base64.b64encode(stream.getvalue()).decode()
    result = ToolResult("Screenshot", images=[
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": original}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "YWJj"}},
    ])
    normalized = await normalize_tool_images(result)
    assert normalized.images[0]["source"]["data"] != original
    assert result.images[0]["source"]["data"] == original
    assert normalized.images[1] == result.images[1]
    assert normalized.content.startswith("Screenshot\n[Image:")
    assert await normalize_tool_images(normalized) == normalized


# 功能：读取工具不再按原始 2MB 阈值拒绝有效图片，实际返回可用压缩附件
# 设计：给有效 PNG 增加大块尾部数据触发编码大小限制，确保不是仅检查像素尺寸
async def test_read_large_valid_image(tmp_path: Path) -> None:
    stream = BytesIO()
    Image.new("RGB", (64, 64), "white").save(stream, format="PNG")
    (tmp_path / "large.png").write_bytes(stream.getvalue() + b"0" * (4 * 1024 * 1024))
    result = await ReadImageTool(WorkspaceBoundary(tmp_path)).invoke({"path": "large.png"})
    assert not result.is_error
    assert result.images
    assert len(result.images[0]["source"]["data"]) < 4_718_592


# 功能：关闭缩放时保留原尺寸，但不支持的模型图片格式仍转换成 PNG
# 设计：用超过默认尺寸的 BMP 验证格式转换与尺寸开关独立，避免把关闭误解为绕过格式转换
async def test_format_conversion_without_resize(tmp_path: Path) -> None:
    stream = BytesIO()
    Image.new("RGB", (2200, 100), "white").save(stream, format="BMP")
    (tmp_path / "image.bmp").write_bytes(stream.getvalue())
    result = await ReadImageTool(WorkspaceBoundary(tmp_path), auto_resize=False).invoke(
        {"path": "image.bmp"},
    )
    assert not result.is_error and result.images
    source = result.images[0]["source"]
    assert source["media_type"] == "image/png"
    with Image.open(BytesIO(base64.b64decode(source["data"]))) as image:
        assert image.size == (2200, 100)
    assert "converted from image/bmp" in result.content
    assert await normalize_tool_images(result, auto_resize=False) == result

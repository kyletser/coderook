# Python port of Pi image-resize-core.ts and tool-result-images.ts (MIT; vendor/pi/LICENSE).
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from io import BytesIO
from typing import TYPE_CHECKING

from PIL import Image, ImageOps

if TYPE_CHECKING:
    from code_rook.core.tools.base import ToolResult


# 保持比例缩放并按 PNG、JPEG 质量阶梯寻找满足 base64 大小限制的编码
def process_image(
    raw: bytes, media_type: str, *, max_dimension: int = 2000,
    max_bytes: int = 4_718_592,
    auto_resize: bool = True,
) -> tuple[str, str, str] | None:
    with Image.open(BytesIO(raw)) as decoded:
        image = ImageOps.exif_transpose(decoded)
        media_type = media_type.split(";")[0].strip().lower().replace("image/jpg", "image/jpeg")
        conversion = ""
        if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            converted = BytesIO()
            image.convert("RGBA").save(converted, format="PNG")
            raw = converted.getvalue()
            conversion = f"[Image converted from {media_type} to image/png.]"
            media_type = "image/png"
        width, height = image.size
        encoded = base64.b64encode(raw).decode("ascii")
        if not auto_resize or (max(width, height) <= max_dimension and len(encoded) < max_bytes):
            return encoded, media_type, conversion
        scale = min(1.0, max_dimension / width, max_dimension / height)
        target = (max(1, round(width * scale)), max(1, round(height * scale)))
        while True:
            resized = image.resize(target, Image.Resampling.LANCZOS)
            for quality in (None, 80, 85, 70, 55, 40):
                buffer = BytesIO()
                if quality is None:
                    resized.save(buffer, format="PNG")
                    mime = "image/png"
                else:
                    resized.convert("RGB").save(buffer, format="JPEG", quality=quality)
                    mime = "image/jpeg"
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
                if len(encoded) < max_bytes:
                    note = (
                        f"[Image: original {width}x{height}, displayed at {target[0]}x{target[1]}. "
                        f"Multiply coordinates by {width / target[0]:.2f} "
                        "to map to original image.]"
                    )
                    return encoded, mime, "\n".join(part for part in (conversion, note) if part)
            if target == (1, 1):
                return None
            target = (max(1, int(target[0] * .75)), max(1, int(target[1] * .75)))


# 在工作线程统一处理工具及扩展的图片，失败保留原块而不静默丢图
async def normalize_tool_images(result: ToolResult, *, auto_resize: bool = True) -> ToolResult:
    if not result.images:
        return result
    normalized = deepcopy(result)
    assert normalized.images is not None
    for block in normalized.images:
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            continue
        try:
            processed = await asyncio.to_thread(
                process_image, base64.b64decode(str(source.get("data", ""))),
                str(source.get("media_type", "image/png")),
                auto_resize=auto_resize,
            )
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        if processed is None:
            continue
        data, mime, note = processed
        source.update(data=data, media_type=mime)
        if note:
            normalized.content += "\n" + note
    return normalized

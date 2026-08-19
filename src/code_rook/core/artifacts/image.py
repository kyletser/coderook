from __future__ import annotations

import struct
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ImageMetadata:
    media_type: str
    width: int
    height: int


class ImageArtifactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(pattern=r"^image/(png|jpeg|webp|gif)$")
    size: int = Field(gt=0, le=2 * 1024 * 1024)
    width: int = Field(gt=0, le=100_000)
    height: int = Field(gt=0, le=100_000)


# 从受支持图片头解析 MIME 与像素尺寸，不依赖可变的文件扩展名
def inspect_image(data: bytes) -> ImageMetadata:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return _validated("image/png", width, height)
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return _validated("image/gif", width, height)
    if data.startswith(b"\xff\xd8"):
        return _inspect_jpeg(data)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return _inspect_webp(data)
    raise ValueError("unsupported or invalid image data")


# 拒绝零尺寸或异常巨大的图片头，避免解码炸弹进入模型请求
def _validated(media_type: str, width: int, height: int) -> ImageMetadata:
    if width < 1 or height < 1 or width > 100_000 or height > 100_000:
        raise ValueError("invalid image dimensions")
    if width * height > 100_000_000:
        raise ValueError("image dimensions exceed 100 megapixels")
    return ImageMetadata(media_type, width, height)


# 扫描 JPEG SOF 段并读取真实像素尺寸
def _inspect_jpeg(data: bytes) -> ImageMetadata:
    offset = 2
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = int.from_bytes(data[offset : offset + 2], "big")
        if length < 2 or offset + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            return _validated("image/jpeg", width, height)
        offset += length
    raise ValueError("JPEG dimensions are unavailable")


# 解析 WebP VP8X、VP8L 或 VP8 chunk 的像素尺寸
def _inspect_webp(data: bytes) -> ImageMetadata:
    if len(data) < 30:
        raise ValueError("truncated WebP image")
    chunk = data[12:16]
    payload = data[20:]
    if chunk == b"VP8X" and len(payload) >= 10:
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return _validated("image/webp", width, height)
    if chunk == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return _validated("image/webp", width, height)
    if chunk == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = int.from_bytes(payload[6:8], "little") & 0x3FFF
        height = int.from_bytes(payload[8:10], "little") & 0x3FFF
        return _validated("image/webp", width, height)
    raise ValueError("WebP dimensions are unavailable")

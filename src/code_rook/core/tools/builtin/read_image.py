from __future__ import annotations

import base64
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect
from code_rook.core.workspace import WorkspaceBoundary

_MAX_IMAGE_BYTES = 2 * 1024 * 1024  # 2 MB
_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ReadImageParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadImageTool(BaseTool):
    params_model = ReadImageParams
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    name = "read_image"
    description = (
        "Read an image file (png/jpg/jpeg/webp/gif) from the workspace and attach it "
        "to the conversation for visual analysis. Use for screenshots, diagrams, and "
        "UI references. Max 2 MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the image file.",
            }
        },
        "required": ["path"],
    }

    # 绑定工作区边界，图片必须位于工作区内
    def __init__(self, boundary: WorkspaceBoundary | None = None) -> None:
        self._boundary = boundary or WorkspaceBoundary(Path.cwd())

    # 读取工作区内图片并编码为 base64 image block，随结果一起返回
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadImageParams.model_validate(params).path
        path = self._boundary.resolve(path_str)
        media_type = _MEDIA_TYPES.get(path.suffix.lower())
        if media_type is None:
            return ToolResult(
                content=(
                    f"unsupported image type {path.suffix!r}; "
                    "allowed: png, jpg, jpeg, webp, gif"
                ),
                is_error=True,
                error_type="schema_error",
            )
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return ToolResult(
                content=f"image not found: {path_str}",
                is_error=True,
                error_type="runtime_error",
            )
        except OSError as exc:
            return ToolResult(
                content=f"image read failed: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        if not raw:
            return ToolResult(
                content=f"image is empty: {path_str}",
                is_error=True,
                error_type="runtime_error",
            )
        if len(raw) > _MAX_IMAGE_BYTES:
            return ToolResult(
                content=(
                    f"image too large: {len(raw)} bytes "
                    f"(max {_MAX_IMAGE_BYTES}); resize or crop it first"
                ),
                is_error=True,
                error_type="runtime_error",
            )
        encoded = base64.b64encode(raw).decode("ascii")
        image_block: dict[str, object] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            },
        }
        rel = path.relative_to(self._boundary.root).as_posix()
        return ToolResult(
            content=(
                f"[image attached: {rel} · {media_type} · {len(raw)} bytes]\n"
                "The image is delivered to you with this result and will be visible "
                "only for the next model call; describe what you observe."
            ),
            images=[image_block],
        )

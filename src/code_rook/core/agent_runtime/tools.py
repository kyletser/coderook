# Python coding-tool surface adapted from Pi (MIT; vendor/pi/LICENSE).
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from code_rook.core.agent_runtime.editing import replace_regions
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.editing.engine import EditEngine, content_hash
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.list_dir import ListDirTool
from code_rook.core.tools.builtin.read_image import ReadImageTool
from code_rook.core.tools.builtin.write_file import WriteFileTool
from code_rook.core.workspace import WorkspaceBoundary, WorkspaceBoundaryError


class ReadParams(BaseModel):
    path: str
    offset: int = Field(default=1, ge=1)
    limit: int = Field(default=2000, ge=1)


class ReadTool(BaseTool):
    name = "read"
    description = (
        "Read text, inspect an image, or list a directory inside the workspace. "
        "Text is capped at 2000 lines or 50KB; use offset/limit to continue."
    )
    prompt_guidelines = (
        "Use read with a directory path to list its contents instead of running ls or find.",
    )
    params_model = ReadParams
    input_schema = ReadParams.model_json_schema()
    side_effect = ToolSideEffect.NONE
    can_parallel = True

    # 绑定项目文件边界以及当前模型的图片能力。
    def __init__(
        self, boundary: WorkspaceBoundary, supports_images: bool = True,
        *, resource_roots: tuple[Path, ...] = (),
        auto_resize: bool = True,
    ) -> None:
        self._boundary = boundary
        self._supports_images = supports_images
        self._auto_resize = auto_resize
        self._resource_boundaries = tuple(WorkspaceBoundary(root) for root in resource_roots)

    # 返回原始文本及继续行号，图片通过相同 read 工具进入多模态结果。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = ReadParams.model_validate(params)
        boundary = self._boundary
        try:
            path = boundary.resolve(request.path)
        except WorkspaceBoundaryError:
            raw = Path(request.path).expanduser()
            candidate = raw if raw.is_absolute() else self._boundary.root / raw
            for resource in self._resource_boundaries:
                try:
                    path = resource.resolve(str(candidate))
                except WorkspaceBoundaryError:
                    continue
                boundary = resource
                break
            else:
                raise
        if path.is_dir():
            relative = path.relative_to(boundary.root).as_posix() or "."
            return await ListDirTool(boundary).invoke({"path": relative, "max_depth": 2})
        if path.suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
        }:
            if not self._supports_images:
                return ToolResult("The current model does not support images.", is_error=True)
            return await ReadImageTool(boundary, auto_resize=self._auto_resize).invoke(
                {"path": str(path)},
            )
        text = path.read_text(encoding="utf-8-sig")
        lines = text.splitlines(keepends=True)
        if request.offset > len(lines) and lines:
            return ToolResult(
                f"Offset {request.offset} exceeds file length ({len(lines)} lines).", is_error=True
            )
        selected: list[str] = []
        size = 0
        for line in lines[request.offset - 1 : request.offset - 1 + min(request.limit, 2000)]:
            count = len(line.encode("utf-8"))
            if size + count > 50 * 1024:
                break
            selected.append(line)
            size += count
        if not selected and request.offset <= len(lines):
            return ToolResult(
                "This line exceeds 50KB. Use the shell to inspect a bounded portion.", is_error=True
            )
        result = "".join(selected)
        next_line = request.offset + len(selected)
        if next_line <= len(lines):
            result += (
                f"\n[Showing lines {request.offset}-{next_line - 1} of {len(lines)}. "
                f"Use offset={next_line} to continue.]"
            )
        return ToolResult(result)


class Replacement(BaseModel):
    oldText: str = Field(min_length=1)
    newText: str


class EditParams(BaseModel):
    path: str
    edits: list[Replacement] = Field(min_length=1)

    # 按 Pi 的编辑参数协议归一化常见模型输出，保留原始调用供账本记录。
    @model_validator(mode="before")
    @classmethod
    def prepare_arguments(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        args = dict(value)
        edits = args.get("edits")
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except json.JSONDecodeError:
                pass
        if (
            isinstance(edits, dict)
            and isinstance(edits.get("oldText"), str)
            and isinstance(edits.get("newText"), str)
        ):
            edits = [edits]
        if isinstance(edits, list):
            args["edits"] = list(edits)
        if isinstance(args.get("oldText"), str) and isinstance(args.get("newText"), str):
            args["edits"] = [
                *(edits if isinstance(edits, list) else []),
                {"oldText": args.pop("oldText"), "newText": args.pop("newText")},
            ]
        return args


# 所有替换都匹配原文，校验唯一且互不重叠后按倒序应用，避免前一编辑影响后一编辑。
def apply_edits(original: str, edits: list[Replacement]) -> str:
    newline = "\r\n" if "\r\n" in original else "\n"
    normalized = original.replace("\r\n", "\n")
    updated = replace_regions(
        normalized,
        [
            (edit.oldText.replace("\r\n", "\n"), edit.newText.replace("\r\n", "\n"))
            for edit in edits
        ],
    )
    return updated.replace("\n", newline)


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Edit a file with edits[{oldText,newText}]. Each oldText matches a unique, "
        "non-overlapping region of the original file, not the result of earlier edits."
    )
    params_model = EditParams
    input_schema = EditParams.model_json_schema()
    side_effect = ToolSideEffect.LOCAL_WRITE

    # 使用既有原子写入和 Checkpoint 实现 Python 文件操作。
    def __init__(
        self, boundary: WorkspaceBoundary, checkpoint_store: CheckpointStore | None
    ) -> None:
        self._boundary = boundary
        self._engine = EditEngine(boundary, checkpoint_store=checkpoint_store)

    # 计算整批修改后一次提交，任何不匹配都不会留下部分修改。
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = EditParams.model_validate(params)
        path = self._boundary.resolve(request.path)
        raw = path.read_bytes()
        original = raw.decode("utf-8")
        updated = apply_edits(original, request.edits)
        outcome = self._engine.edit(
            request.path, original, updated, expected_hash=content_hash(raw)
        )
        payload = asdict(outcome)
        payload["replacements"] = len(request.edits)
        return ToolResult(json.dumps(payload, ensure_ascii=False))


class WriteTool(WriteFileTool):
    name = "write"

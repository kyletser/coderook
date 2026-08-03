from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_rook.core.artifacts import ArtifactError, ArtifactStore
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.spec import OutputPolicy, ToolSpec


class ArtifactReadParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    handle: str | None = Field(default=None, pattern=r"^artifact:[0-9a-f]{64}$")
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20_000, ge=1, le=50_000)

    @model_validator(mode="after")
    # 要求 handle 或兼容 sha256 至少提供一个，且两者同时出现时必须一致
    def validate_reference(self) -> ArtifactReadParams:
        handle_sha = self.handle.removeprefix("artifact:") if self.handle else None
        if handle_sha is None and self.sha256 is None:
            raise ValueError("artifact handle is required")
        if handle_sha is not None and self.sha256 is not None and handle_sha != self.sha256:
            raise ValueError("artifact handle and sha256 do not match")
        return self

    # 返回 handle 中的 hash，并兼容旧调用使用的 sha256
    def resolved_sha256(self) -> str:
        if self.handle is not None:
            return self.handle.removeprefix("artifact:")
        assert self.sha256 is not None
        return self.sha256


class ArtifactReadTool(BaseTool):
    name = "artifact_read"
    description = (
        "Read a verified byte range from a content-addressed tool-output artifact. "
        "Use next_offset to continue without loading the whole artifact."
    )
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    params_model = ArtifactReadParams
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "pattern": "^artifact:[0-9a-f]{64}$",
            },
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50_000},
        },
        "anyOf": [{"required": ["handle"]}, {"required": ["sha256"]}],
    }

    # 绑定当前 workspace 的 artifact store
    def __init__(self, store: ArtifactStore) -> None:
        self._store = store

    # 防止读取 artifact 切片时再次把同一切片 spill 成嵌套 artifact
    def build_spec(self) -> ToolSpec:
        return super().build_spec().model_copy(
            update={
                "output_policy": OutputPolicy(
                    soft_limit=60_000,
                    hard_limit=70_000,
                    spill_to_artifact=False,
                )
            }
        )

    # 读取并返回结构化切片，丢失或损坏时保留错误分类
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            request = ArtifactReadParams.model_validate(params)
            artifact_slice = await self._store.read(
                request.resolved_sha256(),
                offset=request.offset,
                limit=request.limit,
            )
            return ToolResult(
                json.dumps(artifact_slice.model_dump(), ensure_ascii=False, indent=2)
            )
        except ValidationError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
        except ArtifactError as exc:
            return ToolResult(
                json.dumps(
                    {"error": {"code": exc.code, "message": str(exc)}},
                    ensure_ascii=False,
                ),
                is_error=True,
                error_type="runtime_error",
            )

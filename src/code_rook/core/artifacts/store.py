from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_READ_BYTES = 50_000


class ArtifactError(RuntimeError):
    code = "artifact_error"


class ArtifactNotFoundError(ArtifactError):
    code = "artifact_unavailable"


class ArtifactCorruptError(ArtifactError):
    code = "artifact_corrupt"


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    handle: str = Field(pattern=r"^artifact:[0-9a-f]{64}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str
    path: str


class ArtifactSlice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    offset: int = Field(ge=0)
    content: str
    next_offset: int | None = Field(default=None, ge=0)


class ArtifactStore:
    # 初始化内容寻址 artifact 目录及用户可见相对路径前缀
    def __init__(
        self,
        root: Path,
        *,
        display_prefix: str = ".coderook/artifacts",
    ) -> None:
        self._root = root.resolve()
        self._display_prefix = display_prefix.rstrip("/")

    # 验证 artifact ID 并返回不能逃逸根目录的物理路径
    def _path(self, sha256: str) -> Path:
        if not _SHA256_RE.fullmatch(sha256):
            raise ArtifactNotFoundError("invalid artifact sha256")
        return self._root / sha256

    # 同步写入内容寻址文件，已存在时验证 hash 后复用
    def _put_sync(self, data: bytes, media_type: str) -> ArtifactRef:
        if len(data) > _MAX_ARTIFACT_BYTES:
            raise ArtifactError(
                f"artifact exceeds {_MAX_ARTIFACT_BYTES} byte storage limit"
            )
        sha256 = hashlib.sha256(data).hexdigest()
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._path(sha256)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != sha256:
                raise ArtifactCorruptError(f"artifact hash mismatch: {sha256}")
        else:
            handle, temporary_name = tempfile.mkstemp(
                dir=self._root,
                prefix=f".{sha256}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return ArtifactRef(
            handle=f"artifact:{sha256}",
            sha256=sha256,
            size=len(data),
            media_type=media_type,
            path=f"{self._display_prefix}/{sha256}",
        )

    # 异步保存文本或字节内容并返回可验证引用
    async def put(
        self,
        content: str | bytes,
        *,
        media_type: str = "text/plain; charset=utf-8",
    ) -> ArtifactRef:
        data = content.encode("utf-8") if isinstance(content, str) else content
        return await asyncio.to_thread(self._put_sync, data, media_type)

    # 同步校验完整 hash 后读取有界字节范围
    def _read_sync(self, sha256: str, offset: int, limit: int) -> ArtifactSlice:
        if limit < 1 or limit > _MAX_READ_BYTES:
            raise ArtifactError(f"limit must be between 1 and {_MAX_READ_BYTES}")
        path = self._path(sha256)
        if not path.is_file():
            raise ArtifactNotFoundError(f"artifact is unavailable: {sha256}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArtifactCorruptError(f"artifact hash mismatch: {sha256}")
        if offset > len(data):
            raise ArtifactError("artifact offset exceeds its size")
        chunk = data[offset : offset + limit]
        next_offset = offset + len(chunk)
        return ArtifactSlice(
            sha256=sha256,
            size=len(data),
            offset=offset,
            content=chunk.decode("utf-8", errors="replace"),
            next_offset=next_offset if next_offset < len(data) else None,
        )

    # 异步读取经过 hash 校验的有界 artifact 切片
    async def read(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 20_000,
    ) -> ArtifactSlice:
        return await asyncio.to_thread(self._read_sync, sha256, offset, limit)

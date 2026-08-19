from __future__ import annotations

import asyncio
import hashlib
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_READ_BYTES = 50_000
_ARTIFACT_REF_RE = re.compile(rb"artifact:[0-9a-f]{64}")


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


class ArtifactInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    modified_at: datetime
    age_days: float = Field(ge=0)
    referenced: bool
    gc_candidate: bool


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

    # 完整读取并校验小型二进制 artifact，供图片发送等有界场景使用
    def _read_bytes_sync(self, sha256: str, max_bytes: int) -> bytes:
        if max_bytes < 1 or max_bytes > _MAX_ARTIFACT_BYTES:
            raise ArtifactError("invalid artifact byte limit")
        path = self._path(sha256)
        if not path.is_file():
            raise ArtifactNotFoundError(f"artifact is unavailable: {sha256}")
        if path.stat().st_size > max_bytes:
            raise ArtifactError(f"artifact exceeds {max_bytes} byte read limit")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ArtifactCorruptError(f"artifact hash mismatch: {sha256}")
        return data

    # 异步完整读取经过 hash 与大小校验的二进制 artifact
    async def read_bytes(self, sha256: str, *, max_bytes: int) -> bytes:
        return await asyncio.to_thread(self._read_bytes_sync, sha256, max_bytes)

    # 列出可按"年龄 + 引用保留"清理的候选 artifact（仅计算，不删除）
    def list_gc_candidates(
        self,
        *,
        days: int = 30,
        keep: set[str] | None = None,
        now: float | None = None,
    ) -> list[Path]:
        if not self._root.is_dir():
            return []
        retention = days * 24 * 3600
        cutoff = (now if now is not None else time.time()) - retention
        kept = keep or set()
        candidates: list[Path] = []
        for item in sorted(self._root.iterdir(), key=lambda path: path.name):
            if not item.is_file() or not _SHA256_RE.fullmatch(item.name):
                continue
            if item.name in kept:
                continue
            try:
                if item.stat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            candidates.append(item)
        return candidates

    # 列出 artifact 大小、年龄、引用状态和当前 GC 候选判定
    def inventory(
        self,
        *,
        days: int = 30,
        keep: set[str] | None = None,
        now: float | None = None,
    ) -> list[ArtifactInventoryItem]:
        if not self._root.is_dir():
            return []
        current = now if now is not None else time.time()
        kept = keep or set()
        candidate_names = {
            path.name for path in self.list_gc_candidates(days=days, keep=kept, now=current)
        }
        items: list[ArtifactInventoryItem] = []
        for path in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not path.is_file() or not _SHA256_RE.fullmatch(path.name):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                ArtifactInventoryItem(
                    sha256=path.name,
                    size=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    age_days=max(0.0, (current - stat.st_mtime) / 86400),
                    referenced=path.name in kept,
                    gc_candidate=path.name in candidate_names,
                )
            )
        return items

    # 执行 artifact GC；dry_run 只返回候选不清除，默认开启以先展示清单
    def gc(
        self,
        *,
        days: int = 30,
        keep: set[str] | None = None,
        dry_run: bool = True,
        now: float | None = None,
    ) -> list[Path]:
        candidates = self.list_gc_candidates(days=days, keep=keep, now=now)
        if dry_run:
            return candidates
        removed: list[Path] = []
        for path in candidates:
            try:
                path.unlink()
                removed.append(path)
            except OSError:
                continue
        return removed


# 从一批文本/会话文件中提取全部被引用的 artifact sha256，供 GC 的 keep 集合使用
def scan_referenced_artifact_shas(paths: list[Path]) -> set[str]:
    referenced: set[str] = set()
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                overlap = b""
                while chunk := stream.read(1024 * 1024):
                    data = overlap + chunk
                    for match in _ARTIFACT_REF_RE.findall(data):
                        referenced.add(match.decode("ascii").removeprefix("artifact:"))
                    overlap = data[-80:]
        except OSError:
            continue
    return referenced

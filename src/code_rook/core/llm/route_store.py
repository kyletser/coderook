from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_rook.core.daemon_lock import (
    DaemonLock,
    DaemonLockBusyError,
    DaemonLockError,
)
from code_rook.core.llm.routes import ProviderRoute

_DEFAULT_ROUTE_PATH = "~/.coderook/routes.json"
_CURRENT_ROUTE_DOCUMENT_VERSION = 1
_ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class RouteStoreError(ValueError):
    pass


class RouteStoreIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal["invalid_route", "duplicate_route_id", "invalid_active_route"]
    index: int | None = Field(default=None, ge=0)
    route_id: str | None = None
    record_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    quarantined: bool = False


class RouteStoreInspection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_version: int = Field(ge=1)
    declared_active_route_id: str | None = None
    active_route_id: str | None = None
    active_route_unavailable: bool = False
    valid_route_count: int = Field(ge=0)
    issues: tuple[RouteStoreIssue, ...] = ()


@dataclass(frozen=True)
class _RouteLoadResult:
    document: _RouteDocument
    inspection: RouteStoreInspection


@dataclass(frozen=True)
class _RouteStoreSnapshot:
    existed: bool
    content: bytes = b""


class _RouteDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    active_route_id: str | None = None
    routes: list[ProviderRoute] = Field(default_factory=list)

    @model_validator(mode="after")
    # 阻断较新 route 文档被旧版 daemon 静默重写
    def _validate_version(self) -> _RouteDocument:
        if self.version > _CURRENT_ROUTE_DOCUMENT_VERSION:
            raise ValueError(
                "route document version "
                f"{self.version} is newer than supported "
                f"{_CURRENT_ROUTE_DOCUMENT_VERSION}"
            )
        return self

    @model_validator(mode="after")
    # 校验路由 ID 唯一且活动路由必须真实存在
    def _validate_routes(self) -> _RouteDocument:
        ids = [route.id for route in self.routes]
        if len(ids) != len(set(ids)):
            raise ValueError("route ids must be unique")
        if self.active_route_id is not None and self.active_route_id not in ids:
            raise ValueError("active_route_id must reference an existing route")
        return self


class RouteStore:
    # 初始化用户级路由存储路径
    def __init__(self, path: Path | None = None) -> None:
        self.path = (path or Path(_DEFAULT_ROUTE_PATH)).expanduser()
        self._transaction_state = threading.local()

    # 在整个读改写事务期间持有同目录跨进程锁，竞争时失败关闭而不覆盖新状态
    @contextmanager
    def _mutation(self) -> Iterator[None]:
        depth = int(getattr(self._transaction_state, "depth", 0))
        if depth:
            self._transaction_state.depth = depth + 1
            try:
                yield
            finally:
                self._transaction_state.depth = depth
            return
        parent = self.path.parent.absolute()
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise RouteStoreError("route store parent directory is unsafe")
        parent.mkdir(parents=True, exist_ok=True)
        lock_path = parent / f".{self.path.name}.mutation.lock"
        deadline = time.monotonic() + 5.0
        while True:
            lock = DaemonLock(lock_path)
            try:
                lock.acquire()
                break
            except DaemonLockBusyError as exc:
                if time.monotonic() >= deadline:
                    raise RouteStoreError(
                        "provider route catalog is being modified; retry the operation"
                    ) from exc
                time.sleep(0.02)
            except DaemonLockError as exc:
                raise RouteStoreError("provider route catalog lock is unsafe") from exc
        try:
            self._transaction_state.depth = 1
            yield
        finally:
            self._transaction_state.depth = 0
            lock.release()

    # 暴露可组合事务以绑定 Route Catalog 读写与迁移收据且允许同线程安全嵌套
    @contextmanager
    def transaction(self) -> Iterator[RouteStore]:
        with self._mutation():
            yield self

    # 读取并校验路由文档，当前版本逐条隔离坏 route 而未来版本整体失败关闭
    def _load(self) -> _RouteDocument:
        return self._load_result(quarantine_invalid=True).document

    # 解析路由文档并按调用场景选择是否持久隔离坏记录
    def _load_result(self, *, quarantine_invalid: bool) -> _RouteLoadResult:
        if not os.path.lexists(self.path):
            document = _RouteDocument()
            return _RouteLoadResult(
                document=document,
                inspection=RouteStoreInspection(
                    document_version=document.version,
                    active_route_id=document.active_route_id,
                    valid_route_count=0,
                ),
            )
        if self.path.is_symlink() or not self.path.is_file():
            raise RouteStoreError("route store must be a regular file")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._parse_document(raw, quarantine_invalid=quarantine_invalid)
        except RouteStoreError:
            raise
        except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise RouteStoreError(f"invalid route store ({self.path}): {exc}") from exc

    # 严格校验顶层 schema 后逐条保留有效 route 并报告无效 active
    def _parse_document(
        self,
        raw: object,
        *,
        quarantine_invalid: bool,
    ) -> _RouteLoadResult:
        if not isinstance(raw, dict):
            raise ValueError("route document must be an object")
        unknown = set(raw) - {"version", "active_route_id", "routes"}
        if unknown:
            raise ValueError("route document contains unknown fields")
        version = raw.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("route document version must be a positive integer")
        if version > _CURRENT_ROUTE_DOCUMENT_VERSION:
            raise RouteStoreError(
                "route document version "
                f"{version} is newer than supported "
                f"{_CURRENT_ROUTE_DOCUMENT_VERSION}"
            )
        declared_active = raw.get("active_route_id")
        if declared_active is not None and not isinstance(declared_active, str):
            raise ValueError("active_route_id must be a string or null")
        raw_routes = raw.get("routes", [])
        if not isinstance(raw_routes, list):
            raise ValueError("routes must be an array")

        valid_routes: list[ProviderRoute] = []
        route_ids: set[str] = set()
        issues: list[RouteStoreIssue] = []
        for index, candidate in enumerate(raw_routes):
            digest = self._raw_record_digest(candidate)
            route_id = self._safe_route_id(candidate)
            issue_code: Literal["invalid_route", "duplicate_route_id"] | None = None
            try:
                route = ProviderRoute.model_validate(candidate)
                if route.id in route_ids:
                    issue_code = "duplicate_route_id"
                else:
                    route_ids.add(route.id)
                    valid_routes.append(route)
            except (TypeError, ValueError, ValidationError):
                issue_code = "invalid_route"
            if issue_code is not None:
                quarantined = self._route_quarantine_exists(digest)
                if quarantine_invalid and not quarantined:
                    quarantined = bool(
                        self._quarantine_route_record(
                            candidate,
                            index=index,
                            record_digest=digest,
                            reason=issue_code,
                        )
                    )
                issues.append(
                    RouteStoreIssue(
                        code=issue_code,
                        index=index,
                        route_id=route_id,
                        record_digest=digest,
                        quarantined=quarantined,
                    )
                )

        active_route_id = declared_active if declared_active in route_ids else None
        if declared_active is not None and active_route_id is None:
            issues.append(
                RouteStoreIssue(
                    code="invalid_active_route",
                    route_id=(
                        declared_active
                        if _ROUTE_ID_RE.fullmatch(declared_active)
                        else None
                    ),
                )
            )
        document = _RouteDocument(
            version=version,
            active_route_id=active_route_id,
            routes=valid_routes,
        )
        return _RouteLoadResult(
            document=document,
            inspection=RouteStoreInspection(
                document_version=version,
                declared_active_route_id=(
                    declared_active
                    if declared_active is not None
                    and _ROUTE_ID_RE.fullmatch(declared_active)
                    else None
                ),
                active_route_id=active_route_id,
                active_route_unavailable=(
                    declared_active is not None and active_route_id is None
                ),
                valid_route_count=len(valid_routes),
                issues=tuple(issues),
            ),
        )

    # 返回不泄漏原始 route 正文的稳定记录摘要
    def _raw_record_digest(self, candidate: object) -> str:
        encoded = json.dumps(
            candidate,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    # 仅在坏记录仍含合法格式 ID 时返回可安全展示的 route 标识
    def _safe_route_id(self, candidate: object) -> str | None:
        if not isinstance(candidate, dict):
            return None
        route_id = candidate.get("id")
        if not isinstance(route_id, str) or _ROUTE_ID_RE.fullmatch(route_id) is None:
            return None
        return route_id

    # 只读判断指定坏 route 摘要是否已有可恢复的普通隔离文件
    def _route_quarantine_exists(self, record_digest: str) -> bool:
        destination = (
            self.path.parent
            / "_quarantine"
            / f"route-{record_digest}.invalid.json"
        )
        return destination.is_file() and not destination.is_symlink()

    # 将单条坏 route 以内容摘要去重写入受限隔离区并追加脱敏日志
    def _quarantine_route_record(
        self,
        candidate: object,
        *,
        index: int,
        record_digest: str,
        reason: str,
    ) -> Path | None:
        quarantine_dir = self.path.parent / "_quarantine"
        try:
            if quarantine_dir.exists() and (
                quarantine_dir.is_symlink() or not quarantine_dir.is_dir()
            ):
                return None
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(quarantine_dir, 0o700)
            destination = quarantine_dir / f"route-{record_digest}.invalid.json"
            created = False
            if not destination.exists():
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(destination, flags, 0o600)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise OSError("route quarantine target is not a regular file")
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(candidate, ensure_ascii=False, indent=2) + "\n")
                created = True
            elif destination.is_symlink() or not destination.is_file():
                return None
            if created:
                journal = quarantine_dir / "quarantine.jsonl"
                flags = (
                    os.O_CREAT
                    | os.O_APPEND
                    | os.O_WRONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(journal, flags, 0o600)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise OSError("route quarantine journal is not a regular file")
                record = {
                    "schema_version": 1,
                    "category": "provider_route",
                    "source": self.path.name,
                    "index": index,
                    "record_digest": record_digest,
                    "quarantined_name": destination.name,
                    "reason": reason,
                }
                with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            return destination
        except (OSError, TypeError, ValueError):
            return None

    # 只读返回逐 route 校验结果且永不创建隔离文件或重写文档
    def inspect(self) -> RouteStoreInspection:
        return self._load_result(quarantine_invalid=False).inspection

    # 使用同目录临时文件原子替换 Route Catalog 的原始字节
    def _replace_bytes(self, content: bytes) -> None:
        parent = self.path.parent
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise RouteStoreError("route store parent directory is unsafe")
        if os.path.lexists(self.path) and (
            self.path.is_symlink() or not self.path.is_file()
        ):
            raise RouteStoreError("route store must be a regular file")
        parent.mkdir(parents=True, exist_ok=True)
        temporary = parent / f".{self.path.name}.{os.urandom(4).hex()}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                descriptor = -1
                raise OSError("route store temporary target is not a regular file")
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
            raise

    # 使用原子替换写入不含凭据正文的路由文档
    def _save(self, document: _RouteDocument) -> None:
        self._replace_bytes((document.model_dump_json(indent=2) + "\n").encode("utf-8"))

    # 在当前 Route 事务内捕获可精确回滚的原始 Catalog 字节或缺失状态
    def snapshot(self) -> _RouteStoreSnapshot:
        with self._mutation():
            if not os.path.lexists(self.path):
                return _RouteStoreSnapshot(existed=False)
            if self.path.is_symlink() or not self.path.is_file():
                raise RouteStoreError("route store must be a regular file")
            try:
                return _RouteStoreSnapshot(existed=True, content=self.path.read_bytes())
            except OSError as exc:
                raise RouteStoreError("route store snapshot could not be read") from exc

    # 在当前 Route 事务内恢复迁移前的精确 Catalog 字节或缺失状态
    def restore(self, snapshot: _RouteStoreSnapshot) -> None:
        with self._mutation():
            if snapshot.existed:
                self._replace_bytes(snapshot.content)
                return
            if not os.path.lexists(self.path):
                return
            if self.path.is_symlink() or not self.path.is_file():
                raise RouteStoreError("route store rollback target is unsafe")
            try:
                self.path.unlink()
            except OSError as exc:
                raise RouteStoreError("route store rollback could not remove new catalog") from exc

    # 按稳定 ID 排序返回全部已配置路由
    def list(self) -> tuple[ProviderRoute, ...]:
        return tuple(sorted(self._load().routes, key=lambda route: route.id.casefold()))

    # 返回指定路由，不存在时给出明确错误
    def get(self, route_id: str) -> ProviderRoute:
        route = next((item for item in self._load().routes if item.id == route_id), None)
        if route is None:
            raise RouteStoreError(f"route not found: {route_id}")
        return route

    # 新增路由并可选择设为活动路由，禁止静默覆盖
    def add(self, route: ProviderRoute, *, activate: bool = False) -> None:
        with self._mutation():
            document = self._load()
            if any(item.id == route.id for item in document.routes):
                raise RouteStoreError(f"route already exists: {route.id}")
            document.routes.append(route)
            if activate or document.active_route_id is None:
                document.active_route_id = route.id
            self._save(document)

    # 替换同 ID 路由定义，不修改活动路由选择
    def update(self, route: ProviderRoute) -> None:
        with self._mutation():
            document = self._load()
            for index, current in enumerate(document.routes):
                if current.id == route.id:
                    document.routes[index] = route
                    self._save(document)
                    return
            raise RouteStoreError(f"route not found: {route.id}")

    # 在一次原子文件替换中新增或更新 route 并可同步切换活动项
    def commit(
        self,
        route: ProviderRoute,
        *,
        update: bool,
        activate: bool,
    ) -> None:
        with self._mutation():
            document = self._load()
            matching = [
                index for index, item in enumerate(document.routes) if item.id == route.id
            ]
            if update:
                if not matching:
                    raise RouteStoreError(f"route not found: {route.id}")
                document.routes[matching[0]] = route
            else:
                if matching:
                    raise RouteStoreError(f"route already exists: {route.id}")
                document.routes.append(route)
            if activate or document.active_route_id is None:
                document.active_route_id = route.id
            self._save(document)

    # 删除路由并在删除活动项时清空活动选择
    def remove(self, route_id: str) -> None:
        with self._mutation():
            document = self._load()
            remaining = [route for route in document.routes if route.id != route_id]
            if len(remaining) == len(document.routes):
                raise RouteStoreError(f"route not found: {route_id}")
            document.routes = remaining
            if document.active_route_id == route_id:
                document.active_route_id = None
            self._save(document)

    # 将已存在路由设为全局活动路由
    def set_active(self, route_id: str) -> ProviderRoute:
        with self._mutation():
            document = self._load()
            route = next((item for item in document.routes if item.id == route_id), None)
            if route is None:
                raise RouteStoreError(f"route not found: {route_id}")
            document.active_route_id = route_id
            self._save(document)
            return route

    # 返回当前活动路由，未配置时返回空值
    def active(self) -> ProviderRoute | None:
        document = self._load()
        if document.active_route_id is None:
            return None
        return next(
            route for route in document.routes if route.id == document.active_route_id
        )

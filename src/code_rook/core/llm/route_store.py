from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from code_rook.core.llm.routes import ProviderRoute

_DEFAULT_ROUTE_PATH = "~/.coderook/routes.json"


class RouteStoreError(ValueError):
    pass


class _RouteDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    active_route_id: str | None = None
    routes: list[ProviderRoute] = Field(default_factory=list)

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

    # 读取并校验路由文档，文件缺失时返回空文档
    def _load(self) -> _RouteDocument:
        if not self.path.exists():
            return _RouteDocument()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return _RouteDocument.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise RouteStoreError(f"invalid route store ({self.path}): {exc}") from exc

    # 使用原子替换写入不含凭据正文的路由文档
    def _save(self, document: _RouteDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            document.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)

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
        document = self._load()
        if any(item.id == route.id for item in document.routes):
            raise RouteStoreError(f"route already exists: {route.id}")
        document.routes.append(route)
        if activate or document.active_route_id is None:
            document.active_route_id = route.id
        self._save(document)

    # 替换同 ID 路由定义，不修改活动路由选择
    def update(self, route: ProviderRoute) -> None:
        document = self._load()
        for index, current in enumerate(document.routes):
            if current.id == route.id:
                document.routes[index] = route
                self._save(document)
                return
        raise RouteStoreError(f"route not found: {route.id}")

    # 删除路由并在删除活动项时清空活动选择
    def remove(self, route_id: str) -> None:
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

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class CapabilityStability(StrEnum):
    STABLE = "stable"
    LABS = "labs"
    INTERNAL = "internal"


class CapabilityKind(StrEnum):
    TOOL_REGISTRY = "tool_registry"
    MCP_MANAGER = "mcp_manager"
    HOOK_MANAGER = "hook_manager"
    PROVIDER_CATALOG = "provider_catalog"
    WORKER_BACKEND = "worker_backend"


class CapabilityScopeKind(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"
    SESSION = "session"
    WORKER = "worker"


@dataclass(frozen=True)
class CapabilityScope:
    workspace: str = ""
    session: str = ""
    worker: str = ""

    # 返回当前作用域在 global/workspace/session/worker 层级中的深度
    @property
    def depth(self) -> int:
        if self.worker:
            return 3
        if self.session:
            return 2
        if self.workspace:
            return 1
        return 0

    # 判断当前贡献作用域是否是请求作用域的祖先或自身
    def contains(self, requested: CapabilityScope) -> bool:
        if self.workspace and self.workspace != requested.workspace:
            return False
        if self.session and self.session != requested.session:
            return False
        if self.worker and self.worker != requested.worker:
            return False
        return self.depth <= requested.depth


@dataclass(frozen=True)
class CapabilityContribution:
    id: str
    kind: str
    provider: Any
    stability: CapabilityStability = CapabilityStability.INTERNAL
    dependencies: tuple[str, ...] = ()
    scope: CapabilityScope = CapabilityScope()
    priority: int = 0


class ContributionHandle:
    # 保存幂等撤销回调并暴露当前 contribution 是否仍有效
    def __init__(self, contribution: CapabilityContribution, dispose: Callable[[], None]) -> None:
        self.contribution = contribution
        self._dispose = dispose
        self._active = True

    # 返回 contribution 尚未被撤销的状态
    @property
    def active(self) -> bool:
        return self._active

    # 幂等撤销 contribution 及其激活时创建的附属资源
    def dispose(self) -> None:
        if not self._active:
            return
        self._active = False
        self._dispose()


class CapabilityKernel:
    # 初始化轻量贡献表，保留 daemon 现有装配而不引入动态代码加载
    def __init__(self) -> None:
        self._contributions: dict[
            tuple[str, str, CapabilityScope], CapabilityContribution
        ] = {}
        self._cleanups: dict[tuple[str, str, CapabilityScope], Callable[[], None]] = {}
        self._handles: dict[tuple[str, str, CapabilityScope], ContributionHandle] = {}

    # 校验依赖后注册 contribution，并返回能够完整回滚激活副作用的句柄
    def register(
        self,
        contribution: CapabilityContribution,
        *,
        activate: Callable[[Mapping[str, object]], Callable[[], None] | None]
        | None = None,
        context: Mapping[str, object] | None = None,
    ) -> ContributionHandle:
        if not contribution.id or not contribution.kind:
            raise ValueError("capability id and kind are required")
        key = (contribution.kind, contribution.id, contribution.scope)
        if key in self._contributions:
            raise ValueError(
                f"duplicate capability contribution: {contribution.kind}/{contribution.id}"
            )
        missing = [
            dependency
            for dependency in contribution.dependencies
            if not self._dependency_available(dependency, contribution.scope)
        ]
        if missing:
            raise ValueError(f"missing capability dependencies: {', '.join(missing)}")
        cleanup = activate(dict(context or {})) if activate is not None else None
        self._contributions[key] = contribution

        # 先撤销注册，再释放激活资源，避免 cleanup 期间仍被新调用解析
        def dispose() -> None:
            self._contributions.pop(key, None)
            self._handles.pop(key, None)
            registered_cleanup = self._cleanups.pop(key, None)
            if registered_cleanup is not None:
                registered_cleanup()

        if cleanup is not None:
            self._cleanups[key] = cleanup
        handle = ContributionHandle(contribution, dispose)
        self._handles[key] = handle
        return handle

    # 按最近作用域、优先级和稳定 ID 解析一个普通 capability provider
    def resolve(
        self,
        kind: str | CapabilityKind,
        contribution_id: str,
        scope: CapabilityScope,
    ) -> Any | None:
        chain = self.resolve_chain(str(kind), contribution_id, scope)
        return chain[0].provider if chain else None

    # 返回从最近作用域到全局的完整链，供 authority/sandbox/env 调用方执行交集
    def resolve_chain(
        self,
        kind: str | CapabilityKind,
        contribution_id: str,
        scope: CapabilityScope,
    ) -> tuple[CapabilityContribution, ...]:
        resolved_kind = str(kind)
        matches = [
            contribution
            for contribution in self._contributions.values()
            if contribution.kind == resolved_kind
            and contribution.id == contribution_id
            and contribution.scope.contains(scope)
        ]
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    -item.scope.depth,
                    -item.priority,
                    item.id,
                ),
            )
        )

    # 返回指定请求作用域可见的全部 contribution，供 Catalog 和诊断面发现能力
    def list(
        self,
        kind: str | CapabilityKind,
        scope: CapabilityScope,
    ) -> tuple[CapabilityContribution, ...]:
        resolved_kind = str(kind)
        matches = [
            contribution
            for contribution in self._contributions.values()
            if contribution.kind == resolved_kind and contribution.scope.contains(scope)
        ]
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    item.id,
                    -item.scope.depth,
                    -item.priority,
                ),
            )
        )

    # 撤销指定作用域及其后代的全部 contribution 并返回撤销数量
    def dispose_scope(self, scope: CapabilityScope) -> int:
        keys = [
            key
            for key, contribution in self._contributions.items()
            if scope.contains(contribution.scope)
        ]
        for key in keys:
            handle = self._handles.get(key)
            if handle is not None:
                handle.dispose()
        return len(keys)

    # 判断依赖 ID 是否在相同或祖先作用域中可用
    def _dependency_available(self, dependency: str, scope: CapabilityScope) -> bool:
        return any(
            contribution.id == dependency and contribution.scope.contains(scope)
            for contribution in self._contributions.values()
        )

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from code_rook.core.authority import AuthoritySnapshot, RuntimeMode, ToolAction
from code_rook.core.bus.commands import WorkerStartCommand
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.route_registry import (
    ResolvedRoute,
    RouteRegistry,
    RouteResolutionError,
)
from code_rook.core.llm.routes import get_route_preset
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.session import Session, SessionStore
from code_rook.core.subagent.controller import WorkerController, WorkerControllerError
from code_rook.core.subagent.models import WorkerStatus
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.workspace import WorkspaceBoundary


class _Sessions:
    # 保存测试父会话
    def __init__(self, session: Session | None) -> None:
        self.session = session

    # 返回唯一父会话，不存在时模拟 daemon 查找失败
    def get_session(self, session_id: str) -> Session:
        if self.session is None or self.session.id != session_id:
            from code_rook.core.bus.envelope import HandlerError

            raise HandlerError(-32010, "session not found")
        return self.session


class _Routes:
    # 保存冻结 route，并可切换成不可用状态
    def __init__(self, resolved: ResolvedRoute, *, unavailable: bool = False) -> None:
        self.resolved = resolved
        self.unavailable = unavailable
        self.calls: list[tuple[str | None, str | None, str]] = []

    # 按 route/model/digest 返回冻结绑定，任何不一致均失败关闭
    async def resolve_ready(
        self,
        route_id: str | None = None,
        *,
        model: str | None = None,
        expected_digest: str = "",
    ) -> ResolvedRoute:
        self.calls.append((route_id, model, expected_digest))
        if self.unavailable:
            raise RouteResolutionError("route is unavailable")
        route = self.resolved.route
        if route_id is not None and route_id != route.id:
            raise RouteResolutionError("route not found")
        selected = route if model is None else route.model_copy(update={"model": model})
        if expected_digest and expected_digest != selected.validation_digest():
            raise RouteResolutionError("route changed since worker creation")
        return ResolvedRoute(
            route=selected,
            receipt=selected.receipt(self.resolved.receipt.credential_source),
            credential=self.resolved.credential,
        )


class _ResultProvider:
    # 返回一次确定性的无工具调用结果
    async def chat(self, **_kwargs: object) -> LlmResponse:
        return LlmResponse(
            stop_reason="end_turn",
            text="SUMMARY\ndone",
            usage=UsageStats(input_tokens=1, output_tokens=1),
        )


# 构造 controller 及其可观测的持久依赖
def _controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    session: Session | None = None,
    registry: BackgroundTaskRegistry | None = None,
    routes: _Routes | None = None,
) -> tuple[WorkerController, BackgroundTaskRegistry, _Routes, PermissionManager]:
    selected = get_route_preset("ollama")
    resolved = ResolvedRoute(
        route=selected,
        receipt=selected.receipt("missing"),
        credential="",
    )
    route_registry = routes or _Routes(resolved)
    worker_registry = registry or BackgroundTaskRegistry(
        store_path=tmp_path / "workers"
    )
    parent = session or Session(
        id="sess-worker",
        mode="chat",
        status="active",
        title="",
        created_at="2026-08-24T00:00:00+00:00",
        updated_at="2026-08-24T00:00:00+00:00",
        workspace=str(tmp_path.resolve()),
    )
    permissions = PermissionManager(policy_file=tmp_path / "policy.toml")
    monkeypatch.setattr(
        "code_rook.core.subagent.controller.create_provider_for_route",
        lambda *_args, **_kwargs: _ResultProvider(),
    )
    controller = WorkerController(
        registry=worker_registry,
        sessions=cast(Any, _Sessions(parent)),
        session_store=SessionStore(tmp_path / "sessions"),
        route_registry=cast(RouteRegistry, route_registry),
        permission_manager=permissions,
        bus=EventBus(),
        workspace_boundary=WorkspaceBoundary(tmp_path),
        max_steps=3,
    )
    return controller, worker_registry, route_registry, permissions


# 等待指定 Worker 的当前 boot 任务退出
async def _drain(registry: BackgroundTaskRegistry, worker_id: str) -> None:
    live = registry.get(worker_id)
    if live is not None:
        await asyncio.wait_for(live[0], timeout=5)


# 功能：worker.start 通过 daemon launcher 固定 route、权限和持久记录
# 设计：用确定性 provider 完成只读任务，检查 route digest 与 PLAN/READ ceiling 均写入原 WorkerRecord
@pytest.mark.asyncio
async def test_start_persists_frozen_route_and_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, registry, routes, permissions = _controller(tmp_path, monkeypatch)
    permissions.set_authority_snapshot(
        "sess-worker",
        AuthoritySnapshot(
            mode=RuntimeMode.ACT,
            allowed_actions=frozenset({ToolAction.READ, ToolAction.MUTATE}),
        ),
    )

    worker = await controller.start(
        WorkerStartCommand(
            session_id="sess-worker",
            description="inspect repository",
            prompt="inspect repository",
            model="worker-model",
        )
    )
    await _drain(registry, worker.id)
    stored = registry.record(worker.id)

    assert stored is not None
    assert stored.session_id == "sess-worker"
    assert stored.route == routes.resolved.route.id
    assert stored.model == "worker-model"
    assert stored.route_digest == routes.resolved.route.model_copy(
        update={"model": "worker-model"}
    ).validation_digest()
    assert routes.calls[0] == (None, "worker-model", "")
    assert stored.authority_ceiling.mode == RuntimeMode.PLAN
    assert stored.authority_ceiling.allowed_actions == frozenset({ToolAction.READ})


# 功能：写入型 worker.start 只能通过真实受管 worktree 启动
# 设计：在临时 Git 仓库启动 exact-file Worker，验证记录 worktree 且路径位于 .coderook/worktrees
@pytest.mark.asyncio
async def test_start_writing_worker_uses_managed_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=CodeRook Test",
            "-c",
            "user.email=coderook@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    controller, registry, _routes, _permissions = _controller(tmp_path, monkeypatch)

    worker = await controller.start(
        WorkerStartCommand(
            session_id="sess-worker",
            description="edit one file",
            prompt="edit one file",
            read_only=False,
            exact_files=["README.md"],
        )
    )
    await _drain(registry, worker.id)

    assert worker.worktree == worker.id
    assert (tmp_path / ".coderook" / "worktrees" / worker.id).is_dir()
    assert worker.write_claim.exact_files == ["README.md"]


# 功能：父会话不存在时 worker.start 在 route/provider/worktree 之前失败关闭
# 设计：用空 session stub 调用 start，断言 route 未解析且 registry 仍为空
@pytest.mark.asyncio
async def test_start_rejects_missing_parent_session_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = Session(
        id="sess-other",
        mode="chat",
        status="active",
        title="",
        created_at="2026-08-24T00:00:00+00:00",
        updated_at="2026-08-24T00:00:00+00:00",
        workspace=str(tmp_path.resolve()),
    )
    controller, registry, routes, _permissions = _controller(
        tmp_path,
        monkeypatch,
        session=missing,
    )

    with pytest.raises(WorkerControllerError, match="parent session"):
        await controller.start(
            WorkerStartCommand(
                session_id="sess-worker",
                description="must not start",
                prompt="must not start",
            )
        )

    assert routes.calls == []
    assert registry.list_records() == []


# 功能：route readiness 不可用时 worker.start 不创建任何持久或异步 Worker
# 设计：让统一 route stub 抛不可用错误，检查失败发生在 SpawnAgentTool 装配前
@pytest.mark.asyncio
async def test_start_rejects_unavailable_route_without_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = get_route_preset("ollama")
    routes = _Routes(
        ResolvedRoute(
            route=selected,
            receipt=selected.receipt("missing"),
            credential="",
        ),
        unavailable=True,
    )
    controller, registry, _routes, _permissions = _controller(
        tmp_path,
        monkeypatch,
        routes=routes,
    )

    with pytest.raises(RouteResolutionError, match="unavailable"):
        await controller.start(
            WorkerStartCommand(
                session_id="sess-worker",
                description="must not start",
                prompt="must not start",
            )
        )

    assert registry.list_records() == []


# 功能：daemon 重启后的 interrupted Worker 只能以原 route/record 真正启动 attempt 2
# 设计：复用持久 worker store 模拟新 boot，显式 retry 后等待真实后台任务并检查 attempt 与 route 摘要
@pytest.mark.asyncio
async def test_retry_relaunches_recovered_worker_with_original_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = get_route_preset("ollama")
    first = BackgroundTaskRegistry(
        store_path=tmp_path / "workers",
        boot_id="boot-a",
    )
    original = first.new_record(
        worker_id="worker-recovered",
        parent_turn_id="turn-original",
        root_goal_id="worker-recovered",
        session_id="sess-worker",
        description="recover me",
        prompt="recover me",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=3,
        route=selected.id,
        route_digest=selected.validation_digest(),
        model=selected.model,
        retry_backoff_s=0,
    )
    first.create(original)
    second = BackgroundTaskRegistry(
        store_path=tmp_path / "workers",
        boot_id="boot-b",
    )
    controller, registry, routes, _permissions = _controller(
        tmp_path,
        monkeypatch,
        registry=second,
    )

    worker = await controller.retry("sess-worker", original.id)
    await _drain(registry, worker.id)
    stored = registry.record(worker.id)

    assert stored is not None
    assert stored.attempt == 2
    assert stored.route_digest == original.route_digest
    assert routes.calls[-1] == (
        original.route,
        original.model,
        original.route_digest,
    )


# 功能：运行中的 Worker 不能用 retry 绕过状态机创建并发 attempt
# 设计：先启动持久记录但保持 active，再调用 controller.retry 并检查 attempt 不变
@pytest.mark.asyncio
async def test_retry_active_worker_fails_closed_without_attempt_increment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = get_route_preset("ollama")
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    active = registry.new_record(
        worker_id="worker-active",
        parent_turn_id="turn-original",
        root_goal_id="worker-active",
        session_id="sess-worker",
        description="active",
        prompt="active",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=3,
        route=selected.id,
        route_digest=selected.validation_digest(),
        model=selected.model,
        retry_backoff_s=0,
    )
    registry.create(active)
    controller, _registry, _routes, _permissions = _controller(
        tmp_path,
        monkeypatch,
        registry=registry,
    )

    with pytest.raises(WorkerControllerError, match="cannot be resumed or retried"):
        await controller.retry("sess-worker", active.id)

    assert registry.record(active.id).attempt == 1  # type: ignore[union-attr]
    assert registry.record(active.id).status == WorkerStatus.QUEUED  # type: ignore[union-attr]

from __future__ import annotations

import asyncio
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from code_rook.core import app as core_app_module
from code_rook.core.app import CoreApp
from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    ToolAction,
    WorkspaceTrust,
)
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.bus.events import PlanResolvedEvent, VerificationCompletedEvent
from code_rook.core.config import CodeRookConfig, LlmConfig
from code_rook.core.goal import GoalService, GoalStore
from code_rook.core.hooks import HookManager
from code_rook.core.llm.credentials import CredentialStoreError
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.session.model import Session
from code_rook.core.subagent.models import WorkerStatus, WriteClaim
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.worktree import WorktreeManager


# 功能：验证 Core 把通过验证事件关联到当前 Goal，形成只能由 daemon 产生的可信完成证据
# 设计：直接向生产事件处理器发送 typed 事件，核对 run 绑定、证据类型和稳定引用
async def test_goal_event_handler_records_verification(tmp_path: Path) -> None:
    service = GoalService(GoalStore(tmp_path / "goals"))
    goal = service.create(
        "verified and bounded",
        session_id="sess-goal",
        token_budget=1_000,
        completion_criteria=["tests pass"],
    )
    service.start_run(goal.id, "run-goal")
    app = CoreApp()
    app._goal_service = service

    await app._goal_usage_event_handler(
        VerificationCompletedEvent(
            run_id="run-goal",
            step=2,
            tool="Run",
            action="tests",
            gate_count=1,
            passed=1,
            failed=0,
            paths=["src/app.py"],
            gates=[{"name": "pytest", "status": "pass"}],
            ts="2026-01-01T00:00:00+00:00",
        )
    )
    stored = service.get(goal.id)
    assert stored.tokens_used == 0
    assert len(stored.completion_evidence) == 1
    assert stored.completion_evidence[0].kind == "verified-run"
    assert "/verification/2/Run/tests" in stored.completion_evidence[0].reference
    assert stored.completion_evidence[0].covered_criteria == []


# 功能：验证模型自选 gate 名称不能覆盖 Goal 完成标准
# 设计：让 gate 名称与一个标准完全相同，断言只有 daemon 固定 action 可参与覆盖映射
async def test_goal_event_handler_ignores_model_selected_gate_names(
    tmp_path: Path,
) -> None:
    service = GoalService(GoalStore(tmp_path / "goals"))
    goal = service.create(
        "verify exact criteria",
        session_id="sess-goal-exact",
        completion_criteria=["pytest", "all requirements satisfied"],
    )
    service.start_run(goal.id, "run-goal-exact")
    app = CoreApp()
    app._goal_service = service

    await app._goal_usage_event_handler(
        VerificationCompletedEvent(
            run_id="run-goal-exact",
            step=1,
            tool="Run",
            action="tests",
            gate_count=1,
            passed=1,
            failed=0,
            paths=[],
            gates=[{"name": "pytest", "status": "pass"}],
            ts="2026-01-01T00:00:00+00:00",
        )
    )

    stored = service.get(goal.id)
    assert stored.completion_evidence[0].covered_criteria == []


# 功能：session 成本聚合持久 Turn 与子 Agent 用量并对重复 Worker 去重
# 设计：混合已知成本、未知定价和其他会话 Worker，断言只显示已知小计且 token 不重计
async def test_session_usage_summary_includes_workers_without_double_counting() -> None:
    class _Runtime:
        # 返回两个持久 Turn 的用量投影
        async def list_turns(self, thread_id: str) -> list[SimpleNamespace]:
            assert thread_id == "session-cost"
            return [
                SimpleNamespace(
                    usage={
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cache_read_input_tokens": 2,
                        "cache_creation_input_tokens": 1,
                        "estimated_cost_usd": 0.25,
                        "cost_status": "estimated",
                        "models": ["known-model"],
                        "pricing": [{"model": "known-model", "source": "builtin"}],
                    }
                )
            ]

    worker = SimpleNamespace(
        id="worker-1",
        session_id="session-cost",
        input_tokens=7,
        output_tokens=3,
        cache_read_input_tokens=1,
        cache_creation_input_tokens=0,
        token_usage=10,
        model="custom-unpriced",
        cost_status="unknown",
        estimated_cost_usd=None,
    )
    unrelated = SimpleNamespace(
        id="worker-other",
        session_id="other-session",
        input_tokens=999,
        output_tokens=999,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        token_usage=1998,
        model="ignored",
        cost_status="estimated",
        estimated_cost_usd=99.0,
    )

    class _Registry:
        # 返回固定 Worker 集合供两个 registry 路径复用
        def list_records(self) -> list[SimpleNamespace]:
            return [worker, unrelated]

    app = CoreApp()
    app._runtime = _Runtime()  # type: ignore[assignment]
    app._subagent_registry = _Registry()  # type: ignore[assignment]
    app._fleet_registry = _Registry()  # type: ignore[assignment]

    usage = await app._session_usage_summary("session-cost")

    assert usage["input_tokens"] == 17
    assert usage["output_tokens"] == 7
    assert usage["worker_count"] == 1
    assert usage["worker_token_usage"] == 10
    assert usage["known_estimated_cost_usd"] == pytest.approx(0.25)
    assert usage["estimated_cost_usd"] == "unknown"
    assert usage["models"] == ["custom-unpriced", "known-model"]


# 功能：验证 hooks.rerun IPC 把 session_id 传给 trust 边界并诚实报告未执行
# 设计：用捕获参数的轻量 HookManager stub 返回 skipped_untrusted，避免启动任何真实进程
async def test_hook_rerun_handler_forwards_session_trust_scope() -> None:
    calls: list[tuple[str, str]] = []

    class _Hooks:
        # 捕获 hook 与 session 选择器并返回拒绝审计
        async def rerun(self, hook_id: str, *, session_id: str = "") -> Any:
            calls.append((hook_id, session_id))
            return SimpleNamespace(
                status="skipped_untrusted",
                reason="workspace is untrusted",
                ts="2026-08-24T00:00:00Z",
            )

    app = CoreApp()
    app._labs_enabled = True
    app._hooks = _Hooks()  # type: ignore[assignment]

    result = await app._hook_rerun_handler(  # type: ignore[attr-defined]
        {"hook_id": "project-check", "session_id": "sess-untrusted"}
    )

    assert calls == [("project-check", "sess-untrusted")]
    assert result.executed is False
    assert result.status == "skipped_untrusted"


# 功能：验证 Labs 关闭时 Core 构造空 HookManager 且不读取任何工作区配置
# 设计：把 from_workspace 替换为立即失败的哨兵，再断言真实空 manager 的配置集合
def test_disabled_labs_build_an_empty_hook_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = CoreApp()
    app._labs_enabled = False

    # 在禁用路径意外读取磁盘配置时立即使测试失败
    def _unexpected_load(*args: Any, **kwargs: Any) -> HookManager:
        raise AssertionError("disabled Labs must not load hooks")

    monkeypatch.setattr(HookManager, "from_workspace", _unexpected_load)

    manager = app._build_hook_manager(tmp_path)

    assert manager.configs == ()


# 功能：验证 Labs 关闭时 workflow 与 hooks 原始 IPC 全部在触及依赖前拒绝
# 设计：直接调用五个生产 handler 并传入最小合法参数，覆盖隐藏 TUI 无法充当安全边界
async def test_disabled_labs_ipc_handlers_fail_closed() -> None:
    app = CoreApp()
    app._labs_enabled = False

    calls = (
        (app._hooks_list_handler, {"limit": 1}),
        (
            app._hook_rerun_handler,
            {"hook_id": "hook", "session_id": "session"},
        ),
        (
            app._workflow_start_handler,
            {"source": "{}", "format": "json"},
        ),
        (app._workflow_list_handler, {"limit": 1}),
        (app._workflow_get_handler, {"workflow_id": "workflow"}),
    )

    for handler, params in calls:
        with pytest.raises(HandlerError, match="Labs is disabled"):
            await handler(params)


# 功能：验证 runtime capabilities 使用 daemon 启动时冻结的 Labs 状态
# 设计：分别覆盖关闭与开启实例，避免后续环境变量变化让能力协商与真实 handler 漂移
async def test_runtime_capabilities_report_frozen_labs_state() -> None:
    app = CoreApp()
    app._labs_enabled = False
    disabled = await app._runtime_capabilities_handler({})
    app._labs_enabled = True
    enabled = await app._runtime_capabilities_handler({})

    assert disabled.labs_enabled is False
    assert enabled.labs_enabled is True


# 功能：验证 Core 启动子阶段不会把纯空白 API token 直接交给 HTTP server
# 设计：替换文件凭据加载器并调用真实启动辅助方法，断言安全 token 写回冻结配置且成为唯一返回值
def test_core_startup_replaces_blank_runtime_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_paths: list[Path] = []
    app = CoreApp()
    app._config = CodeRookConfig()
    app._config.api.token = "   "

    monkeypatch.setattr(
        core_app_module,
        "load_or_create_api_token",
        lambda path: loaded_paths.append(path) or "generated-api-token",
    )

    token = app._ensure_runtime_api_token()

    assert token == "generated-api-token"
    assert app._config.api.token == token
    assert loaded_paths == [Path("~/.coderook/api-token").expanduser()]


# 功能：验证 Labs 关闭时 daemon 启动路径不会恢复任何持久 workflow
# 设计：用会记录 resume_all 调用的最小 Fleet stub 对照开关两态，避免依赖真实调度进程
def test_disabled_labs_do_not_resume_workflows() -> None:
    calls: list[str] = []

    class _Fleet:
        # 记录恢复调用并返回一个虚拟持久 workflow
        def resume_all(self) -> dict[str, object]:
            calls.append("resume")
            return {"workflow": object()}

    app = CoreApp()
    app._fleet = _Fleet()  # type: ignore[assignment]
    app._labs_enabled = False

    assert app._resume_labs_workflows() == 0
    assert calls == []

    app._labs_enabled = True
    assert app._resume_labs_workflows() == 1
    assert calls == ["resume"]


async def test_agent_run_handler_scopes_and_cleans_headless_mode() -> None:
    manager = PermissionManager()
    checked = asyncio.Event()
    decisions: list[tuple[bool, str]] = []
    session = Session("sess-headless", "one_shot", "active", "", "t", "t")

    class _Sessions:
        async def create(self, mode: str, title: str = "") -> Session:
            return session

        async def send_message(
            self,
            session_id: str,
            content: str,
            *,
            run_id: str | None = None,
        ) -> str:
            async def emit(_event: dict[str, Any]) -> None:
                raise AssertionError("headless permission mode must not request input")

            decisions.append(
                await manager.check_and_wait(
                    tool_use_id="edit-1",
                    tool_name="edit_file",
                    params={"path": "x", "old_text": "a", "new_text": "b"},
                    session_id=session_id,
                    event_emitter=emit,
                )
            )
            checked.set()
            return run_id or ""

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    result = await app._agent_run_handler({  # type: ignore[attr-defined]
        "goal": "edit",
        "permission_mode": "allow_list",
        "allow_tools": ["edit_file"],
    })
    await asyncio.wait_for(checked.wait(), timeout=1)
    await asyncio.sleep(0)

    assert result.run_id
    assert decisions == [(True, "headless_allow_list")]
    assert session.id not in manager._session_modes  # type: ignore[attr-defined]
    assert app._running_runs == set()  # type: ignore[attr-defined]


# 功能：验证会话 authority 更新只替换 mode/profile，并可由查询命令原样读回
# 设计：预置收窄的 action scope 后直接调用 Core handler，确保权限切换不会隐式扩大能力
async def test_session_authority_handlers_preserve_scope() -> None:
    manager = PermissionManager()
    session = Session("sess-authority", "chat", "active", "", "t", "t")
    original = manager.get_authority_snapshot(session.id).model_copy(
        update={"allowed_actions": frozenset({ToolAction.READ, ToolAction.MUTATE})}
    )
    manager.set_authority_snapshot(session.id, original)

    class _Sessions:
        # 返回测试会话并固定存在性校验
        def get_session(self, session_id: str) -> Session:
            assert session_id == session.id
            return session

        # 模拟当前没有运行中的 turn
        def is_busy(self, session_id: str) -> bool:
            assert session_id == session.id
            return False

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    updated = await app._session_set_authority_handler(  # type: ignore[attr-defined]
        {
            "session_id": session.id,
            "mode": "plan",
            "profile": "auto_review",
        }
    )
    restored = await app._session_get_authority_handler(  # type: ignore[attr-defined]
        {"session_id": session.id}
    )

    assert updated.snapshot.mode == RuntimeMode.PLAN
    assert updated.snapshot.profile == AuthorityProfile.AUTO_REVIEW
    assert updated.snapshot.allowed_actions == original.allowed_actions
    assert restored.snapshot == updated.snapshot
    assert manager.get_authority_snapshot("new-session").profile == AuthorityProfile.ASK

    trust_only = await app._session_set_authority_handler(  # type: ignore[attr-defined]
        {"session_id": session.id, "workspace_trust": "trusted"}
    )
    assert trust_only.snapshot.workspace_trust == WorkspaceTrust.TRUSTED
    assert trust_only.snapshot.mode == RuntimeMode.PLAN
    assert trust_only.snapshot.profile == AuthorityProfile.AUTO_REVIEW


# 功能：验证运行中的 turn 不能通过协议静默改变 authority 快照
# 设计：让会话服务明确报告 busy，断言 handler 在写入 PermissionManager 前返回结构化错误
async def test_session_authority_change_rejected_while_turn_is_busy() -> None:
    manager = PermissionManager()
    session = Session("sess-busy", "chat", "active", "", "t", "t")

    class _Sessions:
        # 返回存在的测试会话
        def get_session(self, session_id: str) -> Session:
            assert session_id == session.id
            return session

        # 模拟 turn 正持有执行锁
        def is_busy(self, session_id: str) -> bool:
            assert session_id == session.id
            return True

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    with pytest.raises(HandlerError, match="active turn"):
        await app._session_set_authority_handler(  # type: ignore[attr-defined]
            {"session_id": session.id, "mode": "plan"}
        )
    assert manager.get_authority_snapshot(session.id).mode == RuntimeMode.ACT


# 功能：验证 Change Center 写动作同时要求确认、空闲会话和可信工作区
# 设计：在真正启动 Git 前逐一触发三道 handler 门禁，证明客户端参数不能绕过 daemon 策略
async def test_change_center_mutation_gate_fails_closed() -> None:
    manager = PermissionManager()
    session = Session("sess-change", "chat", "active", "", "t", "t")

    class _Sessions:
        # 返回固定会话供写动作校验存在性
        def get_session(self, session_id: str) -> Session:
            assert session_id == session.id
            return session

        # 返回测试可切换的运行状态
        def is_busy(self, session_id: str) -> bool:
            assert session_id == session.id
            return self.busy

        # 模拟当前工作区没有其他活动 Turn
        def active_run_count(self) -> int:
            return 0

        # 模拟生产读写门闩的独占工作区变更上下文
        @asynccontextmanager
        async def workspace_mutation(self):
            yield

        busy = False

    sessions = _Sessions()
    app = CoreApp()
    app._sessions = sessions  # type: ignore[assignment]
    app._permission_manager = manager  # type: ignore[attr-defined]

    with pytest.raises(HandlerError, match="explicit confirmation"):
        await app._workspace_stage_handler(  # type: ignore[attr-defined]
            {
                "session_id": session.id,
                "paths": ["src/app.py"],
                "expected_digest": "a" * 64,
            }
        )
    with pytest.raises(HandlerError, match="trusted workspace"):
        await app._workspace_commit_handler(  # type: ignore[attr-defined]
            {
                "session_id": session.id,
                "message": "fix",
                "expected_digest": "a" * 64,
                "confirmed": True,
            }
        )

    manager.set_authority_snapshot(
        session.id,
        manager.get_authority_snapshot(session.id).model_copy(
            update={"workspace_trust": WorkspaceTrust.TRUSTED}
        ),
    )
    sessions.busy = True
    with pytest.raises(HandlerError, match="active turn"):
        await app._workspace_stage_handler(  # type: ignore[attr-defined]
            {
                "session_id": session.id,
                "paths": ["src/app.py"],
                "expected_digest": "a" * 64,
                "confirmed": True,
            }
        )


# 功能：Worker handoff 仅在可信空闲工作区经验证、审查和摘要重验后应用并持久化事件
# 设计：使用真实 Git worktree 贯穿 review/apply handler，核对主仓库文件、未暂存状态和 applied 投影
async def test_worker_apply_handler_closes_reviewed_worktree_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".coderook/\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "README.md"],
        check=True,
    )
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
    monkeypatch.chdir(tmp_path)
    worktrees = WorktreeManager(tmp_path)
    base_commit = await worktrees.resolve_ref()
    worker_path = await worktrees.create("core-apply", base_commit)
    (worker_path / "README.md").write_text("applied\n", encoding="utf-8")
    (worker_path / "new.txt").write_text("untracked evidence\n", encoding="utf-8")
    registry = BackgroundTaskRegistry(store_path=tmp_path / ".coderook" / "workers")
    record = registry.new_record(
        worker_id="worker-core-apply",
        parent_turn_id="turn-parent",
        root_goal_id="goal-root",
        session_id="sess-worker-apply",
        description="apply reviewed file",
        prompt="update README",
        workspace=str(worker_path),
        worktree="core-apply",
        branch="coderook/core-apply",
        base_commit=base_commit,
        merge_owner="parent",
        merge_reviewer="reviewer",
        authority_ceiling=AuthoritySnapshot(),
        write_claim=WriteClaim(exact_files=["README.md", "new.txt"]),
        depth=1,
        max_steps=5,
    )
    registry.create(record)
    registry.update_status(
        record.id,
        WorkerStatus.COMPLETED,
        handoff_status="pending_review",
        changed_files=["README.md", "new.txt"],
        verification_status="verified",
    )
    session = Session("sess-worker-apply", "chat", "active", "", "t", "t")

    class _Sessions:
        # 返回应用命令所属的固定会话
        def get_session(self, session_id: str) -> Session:
            assert session_id == session.id
            return session

        # 模拟无活动 Turn 的空闲工作区
        def is_busy(self, session_id: str) -> bool:
            assert session_id == session.id
            return False

        # 模拟全局无其他活动 Turn
        def active_run_count(self) -> int:
            return 0

        # 提供真实 handler 所需的独占变更上下文
        @asynccontextmanager
        async def workspace_mutation(self):
            yield

    permissions = PermissionManager()
    permissions.set_authority_snapshot(
        session.id,
        AuthoritySnapshot(workspace_trust=WorkspaceTrust.TRUSTED),
    )
    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]
    app._permission_manager = permissions  # type: ignore[assignment]
    app._subagent_registry = registry

    preview = await app._worker_review_handler(  # type: ignore[attr-defined]
        {
            "session_id": session.id,
            "worker_id": record.id,
            "approved": True,
            "confirmed": False,
        }
    )
    pending = registry.record(record.id)
    assert preview.preview_only is True
    assert "+applied" in preview.diff
    assert "+untracked evidence" in preview.diff
    assert pending is not None and pending.approved is None
    (worker_path / "new.txt").write_text("changed after preview\n", encoding="utf-8")
    with pytest.raises(HandlerError, match="stale"):
        await app._worker_review_handler(  # type: ignore[attr-defined]
            {
                "session_id": session.id,
                "worker_id": record.id,
                "approved": True,
                "confirmed": True,
                "expected_digest": preview.state_digest,
            }
        )
    preview = await app._worker_review_handler(  # type: ignore[attr-defined]
        {
            "session_id": session.id,
            "worker_id": record.id,
            "approved": True,
            "confirmed": False,
        }
    )
    assert "+changed after preview" in preview.diff
    review = await app._worker_review_handler(  # type: ignore[attr-defined]
        {
            "session_id": session.id,
            "worker_id": record.id,
            "approved": True,
            "confirmed": True,
            "expected_digest": preview.state_digest,
        }
    )
    applied = await app._worker_apply_handler(  # type: ignore[attr-defined]
        {
            "session_id": session.id,
            "worker_id": record.id,
            "expected_digest": review.state_digest,
            "confirmed": True,
        }
    )

    assert applied.handoff_status == "applied"
    assert applied.changed_files == ["README.md", "new.txt"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "applied\n"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "changed after preview\n"
    assert registry.record(record.id).handoff_status == "applied"  # type: ignore[union-attr]
    assert registry.events(record.id)[-1].kind == "worker.handoff_applied"


# 功能：core.shutdown handler 置位停机事件并返回确认
# 设计：直接调用 handler 断言事件从 unset 变 set，覆盖 IPC 优雅停机的触发路径
async def test_shutdown_handler_sets_event() -> None:
    app = CoreApp()
    app._shutdown_event = asyncio.Event()
    result = await app._shutdown_handler({"reason": "test"})  # type: ignore[attr-defined]
    assert result.shutting_down is True
    assert app._shutdown_event.is_set()


# 功能：验证损坏凭据阻断 Provider 迁移时 daemon 转入 audit_degraded 而不退出
# 设计：在 llm_is_configured 边界注入 typed 故障并直接调用启动子阶段，断言诊断模式仍保留 Registry
async def test_provider_migration_failure_enters_diagnostic_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = CoreApp()
    app._config = SimpleNamespace(llm=LlmConfig())  # type: ignore[assignment]

    # 模拟未来或损坏 credentials.json 被严格 loader 拒绝
    def fail_credentials(_config: LlmConfig) -> bool:
        raise CredentialStoreError(
            "unsupported_version",
            tmp_path / "credentials.json",
            "credential document version is newer than supported",
        )

    monkeypatch.setattr(core_app_module, "llm_is_configured", fail_credentials)

    await app._initialize_provider_catalog(tmp_path)

    assert app._route_registry is not None
    assert app._audit_health.degraded is True
    assert app._audit_health.incident is not None
    assert app._audit_health.incident.source == "provider.migration"


# 功能：验证 plan.respond handler 只委托 SessionManager 解决匹配计划并返回 typed 结果
# 设计：用记录型会话替身返回 durable 事件，精确核对 session/run/decision/revision 的透传
async def test_plan_respond_handler_delegates_to_session_manager() -> None:
    calls: list[tuple[str, str, str, str]] = []

    class _Sessions:
        # 记录计划决定并模拟 SessionManager 返回已持久化事件
        async def respond_plan(
            self,
            session_id: str,
            run_id: str,
            decision: str,
            revision: str,
        ) -> PlanResolvedEvent:
            calls.append((session_id, run_id, decision, revision))
            return PlanResolvedEvent(
                session_id=session_id,
                run_id=run_id,
                decision="revise",
                revision=revision,
                ts="2026-08-24T00:00:00Z",
            )

    app = CoreApp()
    app._sessions = _Sessions()  # type: ignore[assignment]

    result = await app._plan_respond_handler(  # type: ignore[attr-defined]
        {
            "session_id": "sess-plan",
            "run_id": "run-plan",
            "decision": "revise",
            "revision": "inspect tests",
        }
    )

    assert calls == [("sess-plan", "run-plan", "revise", "inspect tests")]
    assert result.status == "resolved"
    assert result.run_id == "run-plan"

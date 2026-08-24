from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from code_rook.core.audit import AuditHealth
from code_rook.core.authority import RuntimeMode, ToolAction
from code_rook.core.config import CodeRookConfig
from code_rook.core.goal import GoalService, GoalStore
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import ToolSideEffect
from code_rook.core.tools.builtin.goal_update import GoalUpdateTool


# 创建绑定隔离 GoalStore 的测试工具，并返回同一 service 供状态断言
def _tool(tmp_path: Path) -> tuple[GoalUpdateTool, GoalService, str]:
    service = GoalService(GoalStore(tmp_path / "goals"))
    goal = service.create("verified delivery", session_id="sess-a")
    service.start_run(goal.id, "run-1")
    return GoalUpdateTool(service, goal.id), service, goal.id


# 功能：验证 update_goal 只有引用 daemon 已记录的通过验证后才能完成持久 Goal
# 设计：依次提交空证据、伪造 URI 和 latest-verification，再从真实存储核对唯一可信证据
async def test_goal_update_tool_requires_evidence_for_completion(tmp_path: Path) -> None:
    tool, service, goal_id = _tool(tmp_path)

    with pytest.raises(ValidationError, match="requires at least one evidence"):
        await tool.invoke({"status": "completed", "evidence": []})
    forged = await tool.invoke(
        {
            "status": "completed",
            "evidence": ["tests://unit/green", "git://commit/abc123"],
        }
    )
    assert forged.is_error
    assert "daemon-verified" in forged.content
    verified = service.record_verification(
        goal_id,
        run_id="run-1",
        step=2,
        tool="Run",
        action="tests",
        summary="42 passed",
    )
    completed = await tool.invoke(
        {
            "status": "completed",
            "evidence": ["latest-verification"],
            "summary": "daemon verification accepted",
        }
    )

    assert not completed.is_error
    stored = service.get(goal_id)
    assert stored.status == "completed"
    assert stored.completion_evidence == verified.completion_evidence
    assert stored.completion_evidence[0].kind == "verified-run"


# 功能：验证 update_goal 在权限清单中属于 mutation，Plan 模式不会把它暴露为只读工具
# 设计：直接检查生产 ToolSpec 的副作用、authority action 和 visible_actions，覆盖审计降级共用判定入口
def test_goal_update_tool_is_a_mutating_action(tmp_path: Path) -> None:
    tool, _service, _goal_id = _tool(tmp_path)
    spec = tool.build_spec()

    assert tool.side_effect == ToolSideEffect.LOCAL_WRITE
    assert spec.actions[0].authority_action() == ToolAction.MUTATE
    assert spec.visible_actions(RuntimeMode.PLAN) == ()


# 功能：验证 audit_degraded 会通过 update_goal 的 MUTATE action 拒绝其持久状态写入
# 设计：触发真实 AuditHealth 降级并调用 PermissionManager 静态闸门，避免依赖交互审批超时
async def test_goal_update_is_denied_when_audit_is_degraded(tmp_path: Path) -> None:
    tool, _service, _goal_id = _tool(tmp_path)
    health = AuditHealth()
    await health.degrade("goal-store", OSError("disk full"))
    manager = PermissionManager(
        policy_file=tmp_path / "policy.toml",
        audit_health=health,
    )

    # 降级分支会在发布审批请求前返回，此回调若被调用说明 fail-closed 顺序失效
    async def unexpected_emit(_payload: dict[str, object]) -> None:
        raise AssertionError("audit degraded must not request approval")

    allowed, decision = await manager.check_and_wait(
        tool_use_id="goal-update-1",
        tool_name=tool.name,
        params={"status": "blocked", "summary": "cannot continue"},
        session_id="sess-a",
        event_emitter=unexpected_emit,
        action=tool.build_spec().actions[0].authority_action(),
    )

    assert allowed is False
    assert decision == "audit_degraded"


# 功能：验证 update_goal 标记 blocked 时必须给出阻塞原因且不会伪造完成证据
# 设计：分别调用空原因和有效原因分支，检查错误类型、持久状态和空证据集合
async def test_goal_update_tool_records_explained_blocker(tmp_path: Path) -> None:
    tool, service, goal_id = _tool(tmp_path)

    with pytest.raises(ValidationError, match="requires a summary"):
        await tool.invoke({"status": "blocked", "summary": ""})
    blocked = await tool.invoke(
        {"status": "blocked", "summary": "requires a user-owned credential"}
    )

    assert not blocked.is_error
    stored = service.get(goal_id)
    assert stored.status == "blocked"
    assert stored.status_reason == "requires a user-owned credential"
    assert stored.completion_evidence == []


# 功能：验证 Runner 仅为当前 run 绑定的 active Goal 暴露 update_goal 工具
# 设计：先构建无 Goal 工具表，再启动真实 Goal run 后重建并比较名称，覆盖服务到模型工具目录的装配链
def test_runner_exposes_goal_update_only_for_bound_active_run(tmp_path: Path) -> None:
    service = GoalService(GoalStore(tmp_path / "goals"))
    runner = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
        goal_service=service,
    )
    tasks = TaskManager(tmp_path / ".tasks")

    without_goal = runner._build_registry(
        tasks,
        session_id="sess-a",
        run_id="run-1",
    )
    goal = service.create("ship", session_id="sess-a")
    service.start_run(goal.id, "run-1")
    with_goal = runner._build_registry(
        tasks,
        session_id="sess-a",
        run_id="run-1",
    )

    assert without_goal.get("update_goal") is None
    assert with_goal.get("update_goal") is not None
    assert "update_goal" in {
        str(schema["name"]) for schema in with_goal.tool_schemas()
    }

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from code_rook.core.config import CodeRookConfig
from code_rook.core.goal import GoalService, GoalStore
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.builtin.goal_update import GoalUpdateTool


# 创建绑定隔离 GoalStore 的测试工具，并返回同一 service 供状态断言
def _tool(tmp_path: Path) -> tuple[GoalUpdateTool, GoalService, str]:
    service = GoalService(GoalStore(tmp_path / "goals"))
    goal = service.create("verified delivery", session_id="sess-a")
    service.start_run(goal.id, "run-1")
    return GoalUpdateTool(service, goal.id), service, goal.id


# 功能：验证 update_goal 只有收到具体证据引用后才能显式完成持久 Goal
# 设计：先提交无证据完成请求并检查校验错误，再提交测试报告引用并从真实存储核对终态
async def test_goal_update_tool_requires_evidence_for_completion(tmp_path: Path) -> None:
    tool, service, goal_id = _tool(tmp_path)

    with pytest.raises(ValidationError, match="requires at least one evidence"):
        await tool.invoke({"status": "completed", "evidence": []})
    completed = await tool.invoke(
        {
            "status": "completed",
            "evidence": ["tests://unit/green", "git://commit/abc123"],
            "summary": "tests and commit verified",
        }
    )

    assert not completed.is_error
    stored = service.get(goal_id)
    assert stored.status == "completed"
    assert [item.reference for item in stored.completion_evidence] == [
        "tests://unit/green",
        "git://commit/abc123",
    ]


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

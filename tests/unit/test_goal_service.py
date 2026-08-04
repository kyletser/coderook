from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.goal import GoalService, GoalStore
from code_rook.core.task.models import TaskRecord


# 创建使用隔离目录的 GoalService
def _service(tmp_path: Path) -> GoalService:
    return GoalService(GoalStore(tmp_path / "goals"))


# 功能：验证 Goal 与 Task 是不同模型且目标字段可跨 service 重启恢复
# 设计：创建带预算和约束的 Goal 后重建 service，核对用户级字段并排除 Task 类型混用
def test_goal_is_distinct_and_persists_across_restart(tmp_path: Path) -> None:
    created = _service(tmp_path).create(
        "ship durable control plane",
        token_budget=5000,
        constraints=["no network", "tests pass"],
    )

    restored = _service(tmp_path).get(created.id)

    assert not isinstance(restored, TaskRecord)
    assert restored.objective == "ship durable control plane"
    assert restored.status == "active"
    assert restored.token_budget == 5000
    assert restored.constraints == ["no network", "tests pass"]
    assert restored.timeline[0].event == "goal.created"


# 功能：验证 Goal token 和 elapsed 使用量会累加且不能越过预算
# 设计：先执行一次合法累加，再尝试越界并确认已持久值没有被部分写入
def test_goal_usage_enforces_budget_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("bounded", token_budget=100)

    updated = service.add_usage(goal.id, tokens=60, elapsed_ms=1250)

    assert updated.tokens_used == 60
    assert updated.elapsed_ms == 1250
    with pytest.raises(ValueError, match="token budget exceeded"):
        service.add_usage(goal.id, tokens=41, elapsed_ms=10)
    persisted = service.get(goal.id)
    assert persisted.tokens_used == 60
    assert persisted.elapsed_ms == 1250


# 功能：验证完成 Goal 前必须关联任务并记录可追溯完成证据
# 设计：先断言无证据完成被拒绝，再添加 task/evidence 并检查终态和 timeline
def test_goal_completion_requires_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("verified completion")
    service.link_task(goal.id, 7)

    with pytest.raises(ValueError, match="requires completion evidence"):
        service.set_status(goal.id, "completed")

    service.add_evidence(
        goal.id,
        kind="test-report",
        reference="artifact://pytest/123",
        summary="42 passed",
    )
    completed = service.set_status(goal.id, "completed")

    assert completed.status == "completed"
    assert completed.linked_task_ids == [7]
    assert completed.completion_evidence[0].reference == "artifact://pytest/123"
    assert [entry.event for entry in completed.timeline] == [
        "goal.created",
        "goal.task_linked",
        "goal.evidence_added",
        "goal.status_changed",
    ]


# 功能：验证重复关联同一 Task 不会产生重复 ID 或虚假 timeline
# 设计：连续 link 两次相同 ID，检查集合语义和审计事件数量
def test_goal_task_link_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("link once")

    service.link_task(goal.id, 3)
    restored = service.link_task(goal.id, 3)

    assert restored.linked_task_ids == [3]
    assert [entry.event for entry in restored.timeline].count("goal.task_linked") == 1

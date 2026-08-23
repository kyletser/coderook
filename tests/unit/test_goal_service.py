from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import TypeAdapter

from code_rook.core.bus.commands import Command
from code_rook.core.goal import GoalService, GoalStore, GoalStoreError
from code_rook.core.task.models import TaskRecord


# 创建使用隔离目录的 GoalService
def _service(tmp_path: Path) -> GoalService:
    return GoalService(GoalStore(tmp_path / "goals"))


# 功能：验证 Goal 与 Task 是不同模型且目标字段可跨 service 重启恢复
# 设计：创建带预算和约束的 Goal 后重建 service，核对用户级字段并排除 Task 类型混用
def test_goal_is_distinct_and_persists_across_restart(tmp_path: Path) -> None:
    created = _service(tmp_path).create(
        "ship durable control plane",
        session_id="sess-a",
        token_budget=5000,
        constraints=["no network", "tests pass"],
    )

    restored = _service(tmp_path).get(created.id)

    assert not isinstance(restored, TaskRecord)
    assert restored.objective == "ship durable control plane"
    assert restored.session_id == "sess-a"
    assert restored.status == "active"
    assert restored.token_budget == 5000
    assert restored.constraints == ["no network", "tests pass"]
    assert restored.timeline[0].event == "goal.created"


# 功能：验证 Goal token 和 elapsed 使用量会累加且不能越过预算
# 设计：先执行一次合法累加，再尝试越界并确认已持久值没有被部分写入
def test_goal_usage_enforces_budget_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("bounded", session_id="sess-a", token_budget=100)

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
    goal = service.create("verified completion", session_id="sess-a")
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
    goal = service.create("link once", session_id="sess-a")

    service.link_task(goal.id, 3)
    restored = service.link_task(goal.id, 3)

    assert restored.linked_task_ids == [3]
    assert [entry.event for entry in restored.timeline].count("goal.task_linked") == 1


# 功能：验证 Goal 可暂停、编辑、恢复和清除，且同一 session 不能并存两个未终结目标
# 设计：串联完整控制面状态机并在每一步从磁盘重读，覆盖 UI 重连后的真实持久状态
def test_goal_lifecycle_is_persistent_and_unique_per_session(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "first objective",
        session_id="sess-a",
        completion_criteria=["tests pass"],
    )

    with pytest.raises(ValueError, match="unfinished goal"):
        service.create("second objective", session_id="sess-a")

    paused = service.pause(goal.id)
    assert paused.status == "paused"
    edited = service.edit(goal.id, "edited objective")
    assert edited.objective == "edited objective"
    resumed = service.resume(goal.id)
    assert resumed.status == "active"
    cleared = service.clear(goal.id)

    assert cleared.status == "cleared"
    assert service.current("sess-a") is None
    replacement = service.create("replacement", session_id="sess-a")
    assert replacement.status == "active"


# 功能：验证单轮成功只记录进度而不会误完成长期 Goal，失败时进入可恢复 blocked 状态
# 设计：分别执行成功和失败 run，断言成功 Goal 仍可继续且没有伪证据，失败 Goal 可跨进程恢复
def test_goal_run_outcome_drives_verified_status(tmp_path: Path) -> None:
    service = _service(tmp_path)
    successful = service.create("ship", session_id="sess-success")
    service.start_run(successful.id, "run-1")
    progressed = service.finish_run(successful.id, "run-1", succeeded=True)

    assert progressed.status == "active"
    assert progressed.linked_run_ids == ["run-1"]
    assert progressed.completion_evidence == []
    assert service.current("sess-success") == progressed

    failed = service.create("retry", session_id="sess-failed")
    service.start_run(failed.id, "run-2")
    blocked = service.finish_run(
        failed.id,
        "run-2",
        succeeded=False,
        reason="llm_error",
    )

    assert blocked.status == "blocked"
    assert _service(tmp_path).current("sess-failed") == blocked


# 功能：验证 Goal 只有通过显式证据完成入口才进入 completed，并保留证据摘要
# 设计：先完成一次成功 run，再以测试报告引用原子完成 Goal，排除普通 run 成功的隐式终态转换
def test_goal_complete_requires_explicit_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("ship verified", session_id="sess-a")
    service.start_run(goal.id, "run-1")
    service.finish_run(goal.id, "run-1", succeeded=True)

    with pytest.raises(ValueError, match="requires completion evidence"):
        service.complete(goal.id, evidence=[])

    completed = service.complete(
        goal.id,
        evidence=[("test-report", "artifact://pytest/123")],
        summary="all release tests passed",
    )

    assert completed.status == "completed"
    assert completed.completion_evidence[0].reference == "artifact://pytest/123"
    assert completed.completion_evidence[0].summary == "all release tests passed"
    assert completed.timeline[-1].event == "goal.completed"


# 功能：验证显式完成后取消仍在运行的 turn 不会把 Goal 回退为 blocked
# 设计：在 run 活跃时先写入完成证据，再模拟取消收尾，固定用户执行 /goal complete 的竞态语义
def test_completed_goal_survives_run_cancellation_cleanup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("finish while running", session_id="sess-a")
    service.start_run(goal.id, "run-1")
    service.complete(
        goal.id,
        evidence=[("user-confirmation", "run://run-1")],
        summary="verified by user",
        actor="user",
    )

    finished = service.finish_run(
        goal.id,
        "run-1",
        succeeded=False,
        reason="cancelled",
    )

    assert finished.status == "completed"
    assert finished.current_run_id is None
    assert finished.completion_evidence[0].reference == "run://run-1"


# 功能：验证 daemon 重启会把遗留 run 恢复为 blocked，并允许用户继续恢复
# 设计：模拟进程在 run_started 后退出，再由新 service 执行恢复并检查审计事件和 run 清理
def test_goal_recovery_marks_interrupted_run_blocked(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("survive restart", session_id="sess-a")
    service.start_run(goal.id, "run-crashed")

    recovered = _service(tmp_path).recover_interrupted()

    assert len(recovered) == 1
    assert recovered[0].status == "blocked"
    assert recovered[0].current_run_id is None
    assert recovered[0].timeline[-1].event == "goal.run_recovered"


# 功能：验证 Goal IPC 的八个方法都能通过 type 判别联合解析为对应命令模型
# 设计：直接使用生产 Command TypeAdapter 覆盖 wire discriminator，避免只测各模型而漏掉联合注册
def test_goal_commands_are_registered_in_discriminated_union() -> None:
    adapter = TypeAdapter(Command)
    payloads = [
        {"type": "goal.create", "session_id": "sess-a", "objective": "ship"},
        {"type": "goal.get", "session_id": "sess-a"},
        {"type": "goal.list", "session_id": "sess-a"},
        {
            "type": "goal.edit",
            "session_id": "sess-a",
            "objective": "ship safely",
        },
        {"type": "goal.pause", "session_id": "sess-a"},
        {"type": "goal.resume", "session_id": "sess-a"},
        {"type": "goal.complete", "session_id": "sess-a"},
        {"type": "goal.clear", "session_id": "sess-a"},
    ]

    assert [adapter.validate_python(payload).type for payload in payloads] == [
        payload["type"] for payload in payloads
    ]


# 功能：验证公开 Goal 查询不能通过恶意 ID 逃逸 GoalStore 目录
# 设计：直接调用最低层路径解析并使用父目录片段，确保 IPC 之外仍保持纵深路径防护
def test_goal_store_rejects_path_traversal_ids(tmp_path: Path) -> None:
    store = GoalStore(tmp_path / "goals")

    with pytest.raises(GoalStoreError, match="invalid goal id"):
        store.get("../../sessions/meta")

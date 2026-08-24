from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from code_rook.core.authority import AuthorityProfile, AuthoritySnapshot
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


# 功能：验证单条损坏 Goal 会被隔离且不会阻止其余目标恢复
# 设计：并置一条真实记录和一条非法 JSON，通过重建 service 模拟 daemon 冷启动
def test_invalid_goal_is_quarantined_without_blocking_startup(tmp_path: Path) -> None:
    service = _service(tmp_path)
    valid = service.create("keep running", session_id="sess-valid")
    corrupt = tmp_path / "goals" / "goal-aaaaaaaaaaaa.json"
    corrupt.write_text("{not-json", encoding="utf-8")

    restored = _service(tmp_path).list_all()

    assert [goal.id for goal in restored] == [valid.id]
    assert not corrupt.exists()
    quarantine = tmp_path / "goals" / "_quarantine"
    assert list(quarantine.glob("goal-aaaaaaaaaaaa.*.invalid.json"))
    assert (quarantine / "quarantine.jsonl").is_file()


# 功能：验证旧 Goal schema 读入后会升级为当前版本且后续保存不再伪装旧格式
# 设计：把真实记录降为 v2 并删除新字段，经过 Store get/save 后检查 v4 默认值与磁盘版本
def test_legacy_goal_schema_is_upgraded_before_rewrite(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create("legacy goal", session_id="sess-legacy")
    path = tmp_path / "goals" / f"{created.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    payload.pop("permission_ceiling", None)
    payload.pop("token_reservations", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = GoalStore(tmp_path / "goals")

    restored = store.get(created.id)
    store.save(restored)

    assert restored.schema_version == 4
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 4


# 功能：验证未来 Goal schema 会留在原位而不会被旧 daemon 当损坏记录隔离
# 设计：写入格式完整但 version=99 的文件并调用 list，断言跳过且字节不变
def test_future_goal_schema_is_preserved_in_place(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create("future goal", session_id="sess-future")
    path = tmp_path / "goals" / f"{created.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    original = json.dumps(payload) + "\n"
    path.write_text(original, encoding="utf-8")

    assert GoalStore(tmp_path / "goals").list_all() == []
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "goals" / "_quarantine").exists()


# 功能：验证 Goal 文件名与正文 ID 不一致时会作为损坏记录隔离
# 设计：复制合法正文到另一合法文件名，防止 list/mutate 把跨 ID 记录绑定到错误路径
def test_goal_filename_identity_mismatch_is_quarantined(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create("identity", session_id="sess-id")
    source = tmp_path / "goals" / f"{created.id}.json"
    spoof = tmp_path / "goals" / "goal-aaaaaaaaaaaa.json"
    spoof.write_bytes(source.read_bytes())

    records = GoalStore(tmp_path / "goals").list_all()

    assert [record.id for record in records] == [created.id]
    assert not spoof.exists()


# 功能：验证隔离 symlink 记录时移动的是链接本身而不是其工作区外目标
# 设计：让合法名称指向外部坏文件，触发扫描后检查外部哨兵不变且隔离项仍是 symlink
def test_goal_quarantine_does_not_follow_record_symlink(tmp_path: Path) -> None:
    goals = tmp_path / "goals"
    outside = tmp_path / "outside.json"
    goals.mkdir()
    outside.write_text("{bad", encoding="utf-8")
    link = goals / "goal-aaaaaaaaaaaa.json"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("file symlinks are unavailable on this host")

    assert GoalStore(goals).list_all() == []

    assert outside.read_text(encoding="utf-8") == "{bad"
    assert outside.is_file()
    quarantined = list((goals / "_quarantine").glob("*.invalid.json"))
    assert len(quarantined) == 1
    assert quarantined[0].is_symlink()


# 功能：验证自动 Goal 的有限轮次、墙钟和权限 ceiling 使用安全默认值并跨重启持久化
# 设计：创建时显式开启 auto_continue，其余安全参数走默认值，再用新 service 从磁盘重读完整模型
def test_auto_goal_defaults_and_permission_ceiling_persist(tmp_path: Path) -> None:
    ceiling = AuthoritySnapshot(profile=AuthorityProfile.ASK)
    created = _service(tmp_path).create(
        "bounded loop",
        session_id="sess-auto",
        auto_continue=True,
        completion_criteria=["tests pass"],
        permission_ceiling=ceiling,
    )

    restored = _service(tmp_path).get(created.id)

    assert restored.auto_continue is True
    assert restored.max_auto_turns == 3
    assert restored.max_wall_seconds == 1800
    assert restored.auto_turns_used == 0
    assert restored.auto_window_started_at is not None
    assert restored.permission_ceiling == ceiling
    assert restored.paused_needs_confirmation is False


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


# 功能：验证真实 token 用量达到预算时自动 Goal 立即进入需要确认的暂停态
# 设计：用等于预算的单次 usage 覆盖边界值，并经继续决策核对 typed 剩余额为零
def test_auto_goal_token_budget_exhaustion_pauses_for_confirmation(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "token bounded",
        session_id="sess-token",
        token_budget=10,
        auto_continue=True,
        completion_criteria=["verified"],
    )

    paused = service.record_usage(goal.id, tokens=10)
    decision = service.decide_continue(goal.id)

    assert paused.status == "paused"
    assert paused.paused_reason == "token_budget_exhausted"
    assert paused.paused_needs_confirmation is True
    assert decision.reason == "token_budget_exhausted"
    assert decision.remaining_tokens == 0


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


# 功能：验证 max_auto_turns 统计整个自动窗口中的 Turn 总数而不是额外续轮数
# 设计：设置总上限为二，执行首轮和唯一续轮后断言状态持久化为需确认暂停
def test_auto_goal_decision_is_bounded_by_turns(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "finish bounded work",
        session_id="sess-auto",
        token_budget=1000,
        auto_continue=True,
        max_auto_turns=2,
        completion_criteria=["tests pass"],
    )
    service.start_run(goal.id, "run-initial")
    service.finish_run(goal.id, "run-initial", succeeded=True)

    first = service.decide_continue(goal.id)
    assert first.should_continue is True
    assert first.reason == "ready_for_bounded_continuation"
    assert first.remaining_auto_turns == 1

    service.start_run(goal.id, "run-auto-1")
    service.finish_run(goal.id, "run-auto-1", succeeded=True)
    stopped = service.decide_continue(goal.id)
    persisted = service.get(goal.id)

    assert stopped.should_continue is False
    assert stopped.reason == "max_auto_turns_reached"
    assert stopped.remaining_auto_turns == 0
    assert persisted.status == "paused"
    assert persisted.paused_reason == "max_auto_turns_reached"
    assert persisted.paused_needs_confirmation is True

    resumed = service.resume(goal.id)
    assert resumed.status == "active"
    assert resumed.auto_turns_used == 0
    assert resumed.paused_needs_confirmation is False


# 功能：验证绕过显式 decision API 直接 start_run 也不能突破 Turn 总数上限
# 设计：总上限设为二，不调用第二轮后的 decision 并直接申请第三轮，核对原子暂停
def test_auto_goal_start_run_enforces_limit_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "atomic limit",
        session_id="sess-atomic",
        auto_continue=True,
        max_auto_turns=2,
        completion_criteria=["verified"],
    )
    service.start_run(goal.id, "run-initial")
    service.finish_run(goal.id, "run-initial", succeeded=True)
    service.start_run(goal.id, "run-auto-1")
    service.finish_run(goal.id, "run-auto-1", succeeded=True)

    with pytest.raises(ValueError, match="requires user confirmation"):
        service.start_run(goal.id, "run-auto-2")

    persisted = service.get(goal.id)
    assert persisted.current_run_id is None
    assert persisted.status == "paused"
    assert persisted.paused_reason == "max_auto_turns_reached"


# 功能：验证自动 Goal 的可选完成标准、无标准映射证据及墙钟超限语义
# 设计：分别创建三个隔离 Goal，证明默认 Goal 可有界续跑，普通 artifact 不冒充证据且超时仍暂停
def test_auto_goal_decision_checks_criteria_evidence_and_wall_clock(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    missing = service.create(
        "missing criteria",
        session_id="sess-missing",
        auto_continue=True,
    )
    assert service.decide_continue(missing.id).reason == "ready_for_bounded_continuation"
    assert service.get(missing.id).status == "active"

    evidence = service.create(
        "verify evidence",
        session_id="sess-evidence",
        auto_continue=True,
        completion_criteria=["report verified"],
    )
    service.add_evidence(
        evidence.id,
        kind="test-report",
        reference="artifact://pytest/green",
    )
    evidence_decision = service.decide_continue(evidence.id)
    assert evidence_decision.reason == "ready_for_bounded_continuation"
    assert service.get(evidence.id).status == "active"

    wall = service.create(
        "bounded time",
        session_id="sess-wall",
        auto_continue=True,
        max_wall_seconds=5,
        completion_criteria=["done"],
    )
    started_at = datetime.fromisoformat(wall.auto_window_started_at or "")
    wall_decision = service.decide_continue(
        wall.id,
        now=started_at + timedelta(seconds=5),
    )
    assert wall_decision.reason == "max_wall_seconds_reached"
    assert wall_decision.wall_elapsed_seconds == 5


# 功能：验证部分 verification evidence 继续执行，只有覆盖全部 completion criteria 才暂停请求验收
# 设计：两轮分别绑定一个精确标准，核对首轮仍可续跑、第二轮才产生 completion acceptance 决策
def test_auto_goal_waits_for_complete_criteria_coverage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "cover every criterion",
        session_id="sess-coverage",
        auto_continue=True,
        completion_criteria=["unit tests pass", "lint passes"],
    )
    service.start_run(goal.id, "run-tests")
    service.finish_run(goal.id, "run-tests", succeeded=True)
    tests_evidence = service.record_verification(
        goal.id,
        run_id="run-tests",
        step=1,
        tool="Run",
        action="pytest",
        summary="unit tests pass",
        covered_criteria=["unit tests pass"],
    )

    partial = service.decide_continue(goal.id)
    assert partial.should_continue is True
    assert partial.reason == "ready_for_bounded_continuation"
    assert tests_evidence.status == "active"

    service.start_run(goal.id, "run-lint")
    service.finish_run(goal.id, "run-lint", succeeded=True)
    service.record_verification(
        goal.id,
        run_id="run-lint",
        step=1,
        tool="Run",
        action="ruff",
        summary="lint passes",
        covered_criteria=["lint passes"],
    )
    complete = service.decide_continue(goal.id)

    assert complete.should_continue is False
    assert complete.reason == "completion_evidence_requires_acceptance"
    assert service.get(goal.id).status == "paused"


# 功能：验证 Agent 不能用只覆盖部分标准的 daemon evidence 提前完成 Goal
# 设计：登记一条精确覆盖首项的验证引用，再调用 complete 并核对未满足标准仍保持 active
def test_agent_completion_rejects_partial_criteria_coverage(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "do all checks",
        session_id="sess-partial-complete",
        completion_criteria=["tests", "lint"],
    )
    service.start_run(goal.id, "run-tests")
    service.finish_run(goal.id, "run-tests", succeeded=True)
    evidence = service.record_verification(
        goal.id,
        run_id="run-tests",
        step=1,
        tool="Run",
        action="pytest",
        covered_criteria=["tests"],
    ).completion_evidence[-1]

    with pytest.raises(ValueError, match="unmet completion criteria: lint"):
        service.complete(
            goal.id,
            evidence=[("verified-run", evidence.reference)],
        )

    assert service.get(goal.id).status == "active"


# 功能：验证自动 Goal 不会在 session 权限高于创建时 ceiling 后静默续跑
# 设计：以 ASK 创建 Goal，再用 FULL_ACCESS 快照评估，断言权限提升被持久化为确认暂停
def test_auto_goal_decision_enforces_permission_ceiling(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "stay least privilege",
        session_id="sess-authority",
        auto_continue=True,
        completion_criteria=["verified"],
        permission_ceiling=AuthoritySnapshot(profile=AuthorityProfile.ASK),
    )

    decision = service.decide_continue(
        goal.id,
        current_authority=AuthoritySnapshot(profile=AuthorityProfile.FULL_ACCESS),
    )

    assert decision.should_continue is False
    assert decision.reason == "permission_ceiling_exceeded"
    assert service.get(goal.id).paused_needs_confirmation is True


# 功能：验证自动 Goal 的瞬态模型故障保持 active 并受同一续跑预算约束
# 设计：显式标记 transport failure 为 transient，再走正常决策入口证明不会绕过轮次和权限检查
def test_auto_goal_transient_failure_remains_bounded_and_retryable(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "retry transport once",
        session_id="sess-retry",
        auto_continue=True,
        max_auto_turns=2,
        completion_criteria=["verified"],
    )
    service.start_run(goal.id, "run-1")

    finished = service.finish_run(
        goal.id,
        "run-1",
        succeeded=False,
        reason="transport_error",
        transient_failure=True,
    )
    decision = service.decide_continue(goal.id)

    assert finished.status == "active"
    assert decision.should_continue is True
    assert decision.remaining_auto_turns == 1


# 功能：验证 Goal 只有通过 daemon 验证证据完成入口才进入 completed，并保留证据摘要
# 设计：先完成普通 run 并拒绝伪造 artifact，再记录验证事件并用 latest-verification 原子完成
def test_goal_complete_requires_explicit_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create("ship verified", session_id="sess-a")
    service.start_run(goal.id, "run-1")
    service.finish_run(goal.id, "run-1", succeeded=True)

    with pytest.raises(ValueError, match="requires completion evidence"):
        service.complete(goal.id, evidence=[])
    with pytest.raises(ValueError, match="daemon-verified"):
        service.complete(
            goal.id,
            evidence=[("test-report", "artifact://pytest/123")],
        )

    verified = service.record_verification(
        goal.id,
        run_id="run-1",
        step=3,
        tool="Run",
        action="tests",
        summary="all release tests passed",
    )
    completed = service.complete(
        goal.id,
        evidence=[("verified-run", "latest-verification")],
        summary="all release tests passed",
    )

    assert completed.status == "completed"
    assert completed.completion_evidence == verified.completion_evidence
    assert "/verification/3/Run/tests" in completed.completion_evidence[0].reference
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
    goal = service.create(
        "survive restart",
        session_id="sess-a",
        auto_continue=False,
    )
    service.start_run(goal.id, "run-crashed")

    recovered = _service(tmp_path).recover_interrupted()

    assert len(recovered) == 1
    assert recovered[0].status == "blocked"
    assert recovered[0].current_run_id is None
    assert recovered[0].timeline[-1].event == "goal.run_recovered"


# 功能：验证 daemon 重启后所有未终结自动 Goal 都暂停等待确认，即使当时没有 active run
# 设计：创建未启动的自动 Goal 后重建 service 执行恢复，覆盖普通 interrupted-run 恢复未触及的窗口
def test_auto_goal_recovery_always_requires_confirmation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "resume only after confirmation",
        session_id="sess-auto",
        auto_continue=True,
        completion_criteria=["verified"],
    )

    recovered = _service(tmp_path).recover_interrupted()

    assert [item.id for item in recovered] == [goal.id]
    assert recovered[0].status == "paused"
    assert recovered[0].paused_reason == "daemon_restart_confirmation_required"
    assert recovered[0].paused_needs_confirmation is True
    assert recovered[0].timeline[-1].event == "goal.auto_paused_after_restart"


# 功能：验证 daemon 重启会保守结算遗留 token lease 并清空全部预留
# 设计：在 active run 中直接登记整笔 lease 后重建 service，模拟 usage 未返回时的强杀恢复
def test_goal_recovery_consumes_abandoned_token_leases(tmp_path: Path) -> None:
    service = _service(tmp_path)
    goal = service.create(
        "recover hard budget",
        session_id="sess-budget-restart",
        token_budget=1_000,
        auto_continue=True,
        completion_criteria=["verified"],
    )
    service.start_run(goal.id, "run-crashed")
    lease_id, reserved = service.reserve_token_lease(
        goal.id,
        lease_id="lease-crashed",
    )
    assert (lease_id, reserved) == ("lease-crashed", 1_000)

    recovered = _service(tmp_path).recover_interrupted()[0]

    assert recovered.tokens_used == 1_000
    assert recovered.token_reservations == {}
    assert recovered.current_run_id is None
    assert recovered.status == "paused"
    assert recovered.timeline[-1].details["abandoned_reserved_tokens"] == 1_000


# 功能：验证 Goal IPC 的九个方法都能通过 type 判别联合解析为对应命令模型
# 设计：直接使用生产 Command TypeAdapter 覆盖 wire discriminator，包含有限继续决策以防漏注册
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
        {"type": "goal.continue_decision", "session_id": "sess-a"},
        {"type": "goal.clear", "session_id": "sess-a"},
    ]

    assert [adapter.validate_python(payload).type for payload in payloads] == [
        payload["type"] for payload in payloads
    ]
    created = adapter.validate_python(payloads[0])
    assert created.type == "goal.create"
    assert created.auto_continue is True  # type: ignore[union-attr]
    assert created.max_auto_turns == 3  # type: ignore[union-attr]
    assert created.max_wall_seconds == 1800  # type: ignore[union-attr]


# 功能：验证公开 Goal 查询不能通过恶意 ID 逃逸 GoalStore 目录
# 设计：直接调用最低层路径解析并使用父目录片段，确保 IPC 之外仍保持纵深路径防护
def test_goal_store_rejects_path_traversal_ids(tmp_path: Path) -> None:
    store = GoalStore(tmp_path / "goals")

    with pytest.raises(GoalStoreError, match="invalid goal id"):
        store.get("../../sessions/meta")

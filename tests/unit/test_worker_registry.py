from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.context import ExecutionContext
from code_rook.core.subagent.models import WorkerRecord, WorkerStatus, WriteClaim
from code_rook.core.subagent.registry import (
    BackgroundTaskRegistry,
    WorkerConflictError,
)


# 构造测试所需的最小持久 WorkerRecord
def _record(
    registry: BackgroundTaskRegistry,
    workspace: Path,
    worker_id: str,
    *,
    root_goal_id: str = "goal-root",
    claim: WriteClaim | None = None,
    worktree: str = "",
    merge_owner: str = "",
    merge_reviewer: str = "",
    token_budget: int | None = None,
) -> WorkerRecord:
    if claim is not None and not claim.read_only:
        worktree = worktree or "shared"
        merge_owner = merge_owner or "parent"
        merge_reviewer = merge_reviewer or "reviewer"
    return registry.new_record(
        worker_id=worker_id,
        parent_turn_id="turn-parent",
        root_goal_id=root_goal_id,
        description=f"worker {worker_id}",
        prompt="perform a bounded task",
        workspace=str(workspace),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=10,
        write_claim=claim,
        worktree=worktree,
        merge_owner=merge_owner,
        merge_reviewer=merge_reviewer,
        token_budget=token_budget,
    )


# 功能：daemon 重启后仍可查询 Worker，且旧 boot 的活跃状态恢复为 interrupted
# 设计：两个 registry 复用同一目录并使用不同 boot_id，模拟无内存句柄的真实重启
def test_worker_survives_restart_and_becomes_interrupted(tmp_path: Path) -> None:
    workers = tmp_path / "workers"
    first = BackgroundTaskRegistry(store_path=workers, boot_id="boot-a")
    first.create(_record(first, tmp_path, "worker-1"))

    second = BackgroundTaskRegistry(store_path=workers, boot_id="boot-b")

    restored = second.record("worker-1")
    assert restored is not None
    assert restored.status == WorkerStatus.INTERRUPTED
    assert restored.status_reason == "daemon restarted or worker lease expired"


# 功能：同一 boot 中 Worker 停止心跳并超过 lease 后恢复为 interrupted
# 设计：从持久 heartbeat 精确推进到 timeout 边界，调用 recover_stale 验证无需 daemon 重启也能识别失联
def test_stopped_heartbeat_expires_worker_lease(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(
        store_path=tmp_path / "workers",
        boot_id="boot-current",
    )
    worker = registry.new_record(
        worker_id="worker-stale",
        parent_turn_id="turn-parent",
        root_goal_id="goal-root",
        description="stale worker",
        prompt="stop heartbeats",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=10,
        heartbeat_interval_s=1,
        lease_timeout_s=3,
    )
    registry.create(worker)
    expired_at = datetime.fromisoformat(worker.heartbeat_at) + timedelta(seconds=3)

    recovered = registry.recover_stale(now=expired_at)

    assert [item.id for item in recovered] == [worker.id]
    restored = registry.record(worker.id)
    assert restored is not None
    assert restored.status == WorkerStatus.INTERRUPTED
    assert restored.status_reason == "daemon restarted or worker lease expired"


# 功能：同一 workspace 的两个活跃 Worker 不能声明同一个写入文件
# 设计：先保存第一个 queued claim，再创建相同 exact_files 的 Worker，验证冲突发生在启动前
def test_same_file_write_claim_is_rejected_before_start(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    claim = WriteClaim(exact_files=["src/app.py"])
    registry.create(_record(registry, tmp_path, "worker-a", claim=claim))

    with pytest.raises(WorkerConflictError, match="worker-a"):
        registry.create(_record(registry, tmp_path, "worker-b", claim=claim))


# 功能：文件与其父 write_root 之间的相交声明同样被拒绝
# 设计：分别使用 exact_files 和 write_roots，覆盖非字符串相等的目录包含关系
def test_file_and_write_root_claims_overlap(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    registry.create(
        _record(
            registry,
            tmp_path,
            "worker-a",
            claim=WriteClaim(write_roots=["src"]),
        )
    )

    with pytest.raises(WorkerConflictError):
        registry.create(
            _record(
                registry,
                tmp_path,
                "worker-b",
                claim=WriteClaim(exact_files=["src/app.py"]),
            )
        )


# 功能：只读 Worker 无需写入 claim 且不会与写入 Worker 冲突
# 设计：用默认 read_only claim 与写 claim 共存，证明只读分析不占用写租约
def test_read_only_worker_needs_no_write_claim(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    registry.create(_record(registry, tmp_path, "reader"))
    registry.create(
        _record(
            registry,
            tmp_path,
            "writer",
            claim=WriteClaim(exact_files=["src/app.py"]),
        )
    )
    assert len(registry.list_records()) == 2


# 功能：真实独立 worktree 可持有相同相对路径 claim，但必须声明合并 owner 和 reviewer
# 设计：使用两个不同 workspace 根模拟独立 worktree，并验证缺少合并责任人时模型校验失败
def test_independent_worktrees_require_merge_owners(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    claim = WriteClaim(exact_files=["src/app.py"])
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    registry.create(
        _record(
            registry,
            left,
            "worker-left",
            claim=claim,
            worktree="left",
            merge_owner="parent",
            merge_reviewer="reviewer",
        )
    )
    registry.create(
        _record(
            registry,
            right,
            "worker-right",
            claim=claim,
            worktree="right",
            merge_owner="parent",
            merge_reviewer="reviewer",
        )
    )
    with pytest.raises(ValidationError, match="merge owner and reviewer"):
        registry.new_record(
            worker_id="worker-invalid",
            parent_turn_id="turn-parent",
            root_goal_id="goal-root",
            description="invalid merge contract",
            prompt="perform a bounded task",
            workspace=str(right),
            authority_ceiling=AuthoritySnapshot(),
            depth=1,
            max_steps=10,
            write_claim=claim,
            worktree="invalid",
        )


# 功能：heartbeat interval 必须严格小于 lease timeout
# 设计：构造相等边界值，验证配置在 WorkerRecord 创建阶段即被拒绝
def test_heartbeat_interval_must_be_less_than_lease(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    with pytest.raises(ValidationError, match="heartbeat interval"):
        registry.new_record(
            worker_id="worker-lease",
            parent_turn_id="turn-parent",
            root_goal_id="goal-root",
            description="lease test",
            prompt="test lease validation",
            workspace=str(tmp_path),
            authority_ceiling=AuthoritySnapshot(),
            depth=1,
            max_steps=10,
            heartbeat_interval_s=30,
            lease_timeout_s=30,
        )


# 功能：根 goal token budget 在所有 descendant 间共享并产生 budget_limited 终态
# 设计：两个 Worker 分别消费一半预算，第二次累加达到上限时断言两者同时停止
def test_root_token_budget_limits_all_descendants(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    registry.create(
        _record(registry, tmp_path, "worker-a", token_budget=10)
    )
    registry.create(
        _record(registry, tmp_path, "worker-b", token_budget=10)
    )

    assert registry.add_token_usage("worker-a", 4) is False
    assert registry.add_token_usage("worker-b", 6) is True
    assert registry.record("worker-a").status == WorkerStatus.BUDGET_LIMITED  # type: ignore[union-attr]
    assert registry.record("worker-b").status == WorkerStatus.BUDGET_LIMITED  # type: ignore[union-attr]


# 功能：retry 同时受最大次数和指数 backoff 约束，不能无限立即重试
# 设计：分别构造 max_attempts=1 与正 backoff 的失败 Worker，检查 prepare_retry 明确拒绝
def test_retry_respects_attempt_limit_and_backoff(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    limited = registry.new_record(
        worker_id="worker-limited",
        parent_turn_id="turn-parent",
        root_goal_id="goal-limited",
        description="limited retry",
        prompt="fail once",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=5,
        max_attempts=1,
    )
    delayed = registry.new_record(
        worker_id="worker-delayed",
        parent_turn_id="turn-parent",
        root_goal_id="goal-delayed",
        description="delayed retry",
        prompt="wait before retry",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=5,
        retry_backoff_s=60,
    )
    registry.create(limited)
    registry.create(delayed)
    registry.update_status("worker-limited", WorkerStatus.FAILED, reason="failed")
    registry.update_status("worker-delayed", WorkerStatus.FAILED, reason="failed")

    with pytest.raises(ValueError, match="retry limit"):
        registry.prepare_retry("worker-limited")
    with pytest.raises(ValueError, match="backoff active"):
        registry.prepare_retry("worker-delayed")


# 功能：同一 root goal 的 Worker 自动继承唯一共享 token budget
# 设计：先创建无预算 sibling，再创建有预算 sibling，验证 registry 回填并拒绝冲突预算
def test_root_goal_budget_is_single_shared_ledger(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    registry.create(_record(registry, tmp_path, "worker-a"))
    registry.create(_record(registry, tmp_path, "worker-b", token_budget=20))

    assert registry.record("worker-a").token_budget == 20  # type: ignore[union-attr]
    with pytest.raises(WorkerConflictError, match="share one token budget"):
        registry.create(_record(registry, tmp_path, "worker-c", token_budget=30))


# 功能：WorkerStore 持久化细分 token、缓存用量和可解释的模型成本
# 设计：使用内置有单价的模型累计一次 usage，再重启 registry 验证字段不依赖进程内计数
def test_worker_llm_usage_and_estimated_cost_survive_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "workers"
    registry = BackgroundTaskRegistry(store_path=store_path, boot_id="boot-a")
    worker = _record(registry, tmp_path, "worker-usage")
    worker.model = "gpt-5.6"
    registry.create(worker)

    exhausted = registry.add_llm_usage(
        worker.id,
        input_tokens=100,
        output_tokens=20,
        cache_read_input_tokens=30,
        cache_creation_input_tokens=10,
        model="gpt-5.6",
    )
    restored = BackgroundTaskRegistry(
        store_path=store_path,
        boot_id="boot-a",
        recover=False,
    ).record(worker.id)

    assert exhausted is False
    assert restored is not None
    assert restored.token_usage == 120
    assert restored.input_tokens == 100
    assert restored.output_tokens == 20
    assert restored.cache_read_input_tokens == 30
    assert restored.cache_creation_input_tokens == 10
    assert restored.cost_status == "estimated"
    assert restored.estimated_cost_usd is not None
    assert restored.estimated_cost_usd > 0


# 功能：人工 review 只推进 handoff 审查状态，绝不写成已 apply
# 设计：先构造已完成且待审查的写 Worker，再批准并检查持久状态与审计事件
def test_worker_review_records_approval_without_apply(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    worker = _record(
        registry,
        tmp_path,
        "worker-review",
        claim=WriteClaim(exact_files=["src/app.py"]),
    )
    registry.create(worker)
    registry.update_status(
        worker.id,
        WorkerStatus.COMPLETED,
        handoff_status="pending_review",
        changed_files=["src/app.py"],
    )

    reviewed = registry.review_handoff(
        worker.id,
        approved=True,
        review_digest="a" * 64,
    )

    assert reviewed.approved is True
    assert reviewed.handoff_status == "reviewed_not_applied"
    assert reviewed.review_digest == "a" * 64
    assert reviewed.changed_files == ["src/app.py"]
    assert registry.events(worker.id)[-1].kind == "worker.handoff_reviewed"


# 功能：Worker apply 仅接受完成、真实验证且绑定人工审查摘要的 handoff
# 设计：从待审查记录逐步补齐 verified 与 approval，再用同一摘要推进 applied 并检查持久事件
def test_worker_apply_requires_verified_digest_bound_review(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    worker = _record(
        registry,
        tmp_path,
        "worker-apply",
        claim=WriteClaim(exact_files=["src/app.py"]),
    )
    registry.create(worker)
    registry.update_status(
        worker.id,
        WorkerStatus.COMPLETED,
        handoff_status="pending_review",
        changed_files=["src/app.py"],
        verification_status="reported_unverified",
    )
    registry.review_handoff(
        worker.id,
        approved=True,
        review_digest="b" * 64,
    )

    with pytest.raises(ValueError, match="daemon-verified"):
        registry.require_applicable_handoff(
            worker.id,
            expected_digest="b" * 64,
        )

    registry.update_status(
        worker.id,
        WorkerStatus.COMPLETED,
        verification_status="verified",
    )
    applied = registry.mark_handoff_applied(
        worker.id,
        state_digest="b" * 64,
        changed_files=["src/app.py"],
    )
    assert applied.handoff_status == "applied"
    assert registry.events(worker.id)[-1].kind == "worker.handoff_applied"


# 功能：协调文本或截断检查不能替代可枚举的 Worker 文件 claim
# 设计：分别构造无路径 coordination claim 与截断 diff，验证审批阶段即 fail closed
def test_worker_review_rejects_unbounded_or_truncated_handoff(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    unbounded = _record(
        registry,
        tmp_path,
        "worker-unbounded",
        claim=WriteClaim(coordination_contract="coordinate with parent"),
    )
    truncated = _record(
        registry,
        tmp_path,
        "worker-truncated",
        claim=WriteClaim(write_roots=["src"]),
        worktree="truncated",
    )
    registry.create(unbounded)
    registry.create(truncated)
    registry.update_status(
        unbounded.id,
        WorkerStatus.COMPLETED,
        handoff_status="pending_review",
        changed_files=["src/app.py"],
    )
    registry.update_status(
        truncated.id,
        WorkerStatus.COMPLETED,
        handoff_status="pending_review",
        changed_files=["src/app.py"],
        diff_truncated=True,
    )

    with pytest.raises(ValueError, match="write claim"):
        registry.review_handoff(
            unbounded.id,
            approved=True,
            review_digest="c" * 64,
        )
    with pytest.raises(ValueError, match="truncated"):
        registry.review_handoff(
            truncated.id,
            approved=True,
            review_digest="d" * 64,
        )


# 功能：daemon shutdown 取消活跃任务时持久终态为 interrupted 而不是用户 cancelled
# 设计：注册真实阻塞 asyncio.Task，先切换 shutdown 模式再 cancel_all，模拟正常服务关闭顺序
async def test_daemon_shutdown_interrupts_live_worker(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    worker = _record(registry, tmp_path, "worker-shutdown")
    registry.create(worker)

    # 保持测试任务运行直到 registry 在 shutdown 路径取消它
    async def forever() -> None:
        await asyncio.Event().wait()

    context = ExecutionContext(
        run_id=worker.id,
        goal=worker.prompt,
        max_steps=worker.max_steps,
    )
    task = asyncio.create_task(forever())
    registry.register(worker.id, task, context, parent_run_id=worker.parent_turn_id)
    registry.begin_shutdown()
    await registry.cancel_all()

    restored = registry.record(worker.id)
    assert task.cancelled()
    assert restored is not None
    assert restored.status == WorkerStatus.INTERRUPTED
    assert restored.status_reason == "daemon_shutdown"

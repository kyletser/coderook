from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_rook.core.subagent.models import WriteClaim
from code_rook.core.subagent.planning import DelegationPlan, DelegationTask


# 构造具有独立写入目录和验收条件的委派任务
def _task(task_id: str, root: str, *, dependencies: tuple[str, ...] = ()) -> DelegationTask:
    return DelegationTask(
        id=task_id,
        role="executor",
        prompt=f"implement {task_id}",
        dependencies=dependencies,
        write_claim=WriteClaim(write_roots=[root]),
        acceptance=("targeted tests pass",),
        token_budget=1_000,
    )


# 功能：验证无依赖且写入范围不重叠的 Worker 被安排在同一并行波次
# 设计：构造两个独立目录声明，直接比较稳定拓扑波次避免依赖调度时序
def test_delegation_plan_parallelizes_disjoint_claims() -> None:
    plan = DelegationPlan(
        tasks=(_task("api", "src/api"), _task("tui", "src/tui")),
        total_token_budget=2_000,
    )

    assert plan.execution_waves() == (("api", "tui"),)


# 功能：验证父子目录 Write Claim 冲突在启动 Worker 前被百分百拦截
# 设计：用目录包含关系构造最小冲突，断言 Pydantic 校验阶段失败关闭
def test_delegation_plan_rejects_overlapping_write_claims() -> None:
    with pytest.raises(ValidationError, match="write claim conflict"):
        DelegationPlan(
            tasks=(_task("root", "src"), _task("child", "src/api")),
            total_token_budget=2_000,
        )


# 功能：验证依赖环和嵌套委派均不能进入执行阶段
# 设计：分别构造双节点环和显式嵌套标志，覆盖两条确定性安全边界
def test_delegation_plan_rejects_cycles_and_nested_delegation() -> None:
    with pytest.raises(ValidationError, match="dependency cycle"):
        DelegationPlan(
            tasks=(
                _task("a", "src/a", dependencies=("b",)),
                _task("b", "src/b", dependencies=("a",)),
            ),
            total_token_budget=2_000,
        )
    with pytest.raises(ValidationError, match="nested delegation"):
        DelegationPlan(
            tasks=(_task("a", "src/a"),),
            total_token_budget=1_000,
            allow_nested_delegation=True,
        )

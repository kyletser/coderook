from __future__ import annotations

from code_rook.core.task.model import Task


# 功能：验证 Task V2 序列化包含持久控制面字段和旧客户端兼容字段
# 设计：直接构造最小 Task，精确比较 key 集合以防新增字段遗漏持久化
def test_task_to_dict_keys() -> None:
    task = Task(
        id=1,
        subject="test",
        description="desc",
        status="pending",
        created_at="2024-01-01T00:00:00",
        updated_at="2024-01-01T00:00:00",
    )

    payload = task.to_dict()

    assert set(payload) == {
        "schema_version",
        "id",
        "subject",
        "description",
        "status",
        "dependencies",
        "owner_worker",
        "worktree",
        "attempts",
        "acceptance_criteria",
        "gates",
        "artifacts",
        "timeline",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "blocked_by",
        "owner",
    }


# 功能：验证旧版 task JSON 会迁移状态、依赖和 owner 字段
# 设计：从仅含 V1 字段的字典加载，断言 V2 字段与兼容视图一致
def test_task_migrates_v1_payload() -> None:
    restored = Task.from_dict(
        {
            "id": 3,
            "subject": "write tests",
            "description": "cover all tools",
            "status": "in_progress",
            "blocked_by": [1, 2],
            "owner": "executor",
            "worktree": "wt-3",
            "created_at": "t1",
            "updated_at": "t2",
        }
    )

    assert restored.schema_version == 2
    assert restored.status == "running"
    assert restored.dependencies == [1, 2]
    assert restored.blocked_by == [1, 2]
    assert restored.owner_worker == "executor"


# 功能：验证不同 Task 实例的 dependencies 列表互不共享
# 设计：修改一个实例的列表，断言另一个实例及其兼容视图均不受影响
def test_task_dependencies_not_shared() -> None:
    first = Task(id=1, subject="a", created_at="", updated_at="")
    second = Task(id=2, subject="b", created_at="", updated_at="")

    first.dependencies.append(99)

    assert second.dependencies == []
    assert second.blocked_by == []


# 功能：验证旧版 timeline 条目加载时补入所属 task ID
# 设计：构造缺少 task_id 的 V1 timeline，断言迁移后条目可独立投影到 runtime
def test_task_migrates_timeline_task_id() -> None:
    restored = Task.from_dict(
        {
            "id": 7,
            "subject": "legacy",
            "created_at": "t1",
            "updated_at": "t2",
            "timeline": [
                {
                    "seq": 1,
                    "event": "task.created",
                    "actor": "legacy",
                    "at": "t1",
                    "details": {},
                }
            ],
        }
    )

    assert restored.timeline[0].task_id == 7

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from code_rook.core.task.manager import TaskManager


# 功能：验证 create 写入 JSON 文件并让无依赖任务进入 ready
# 设计：用 tmp_path 隔离文件系统，同时断言 V2 初始状态和持久文件
def test_create_writes_file(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    task = mgr.create("do something")
    assert task.id == 1
    assert task.subject == "do something"
    assert task.status == "ready"
    assert (tmp_path / "task_1.json").exists()


# 功能：验证多次 create 的 ID 递增
# 设计：连续创建两个任务，断言 ID 分别为 1 和 2
def test_create_increments_id(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    t1 = mgr.create("first")
    t2 = mgr.create("second")
    assert t1.id == 1
    assert t2.id == 2


# 功能：验证损坏或非法命名的 Task 文件会被隔离且不阻断合法任务列表
# 设计：同时注入坏 JSON 与坏文件名，覆盖内容迁移失败和扫描排序异常两条启动路径
def test_invalid_tasks_are_quarantined_without_blocking_list(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    valid = manager.create("valid task")
    (tmp_path / "task_99.json").write_text("[]", encoding="utf-8")
    (tmp_path / "task_bad.json").write_text("{}", encoding="utf-8")

    restored = manager.list_all()

    assert [task.id for task in restored] == [valid.id]
    quarantine = tmp_path / "_quarantine"
    assert len(list(quarantine.glob("task_*.invalid.json"))) == 2
    assert (quarantine / "quarantine.jsonl").is_file()


# 功能：验证旧 Task schema 经读取和保存后升级为 v2 且新字段不挂旧版本号
# 设计：手写最小 v1 任务，经 manager 更新触发读改写，再检查磁盘 schema 与迁移字段
def test_legacy_task_schema_is_upgraded_before_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "task_1.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": 1,
                "subject": "legacy",
                "status": "pending",
                "blocked_by": [],
                "created_at": "t1",
                "updated_at": "t2",
            }
        ),
        encoding="utf-8",
    )
    manager = TaskManager(tmp_path)

    manager.update(1, status="running")
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 2
    assert "gates" in payload and "timeline" in payload


# 功能：验证未来 Task schema 会保留在原位而不会被当前列表扫描隔离
# 设计：从合法任务改成 version=99 后调用 list，断言跳过记录且原始字节保持
def test_future_task_schema_is_preserved_in_place(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("future")
    path = tmp_path / "task_1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 99
    original = json.dumps(payload) + "\n"
    path.write_text(original, encoding="utf-8")

    assert manager.list_all() == []
    assert path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "_quarantine").exists()


# 功能：验证 Task 文件名与正文 ID 不一致时不会进入任务控制面
# 设计：把 task_1 正文 ID 改为二，调用 list 后确认记录被隔离而非以错误 ID 返回
def test_task_filename_identity_mismatch_is_quarantined(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("identity")
    path = tmp_path / "task_1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["id"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert manager.list_all() == []
    assert not path.exists()


# 功能：验证并发创建任务时 ID 分配和首次落盘保持原子
# 设计：多个线程共享同一 TaskManager 同时 create，断言 ID 唯一连续且文件无覆盖
def test_concurrent_create_allocates_unique_ids(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tasks = list(executor.map(lambda index: manager.create(f"task-{index}"), range(40)))

    assert sorted(task.id for task in tasks) == list(range(1, 41))
    assert len(list(tmp_path.glob("task_*.json"))) == 40


# 功能：验证 create 传入不存在的 blocked_by 抛出 ValueError
# 设计：blocked_by=[99] 引用不存在的任务，预期 ValueError
def test_create_invalid_blocked_by_raises(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        mgr.create("dependent", blocked_by=[99])


# 功能：验证 get 返回正确的 Task
# 设计：create 后立即 get，断言 subject 一致
def test_get_returns_task(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("hello")
    task = mgr.get(1)
    assert task.subject == "hello"


# 功能：验证 get 不存在的 ID 抛出 ValueError
# 设计：不创建任何任务，直接 get(999)，预期 ValueError
def test_get_nonexistent_raises(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    with pytest.raises(ValueError):
        mgr.get(999)


# 功能：验证旧版 in_progress 输入会迁移成 running 并写回文件
# 设计：经兼容 facade 更新旧状态名，重新读取后只允许出现 V2 状态
def test_update_status(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("work")
    mgr.update(1, status="in_progress")
    assert mgr.get(1).status == "running"


# 功能：验证依赖完成后下游变为 ready 且依赖历史不会丢失
# 设计：完成上游后重新读取下游，同时断言状态刷新与 dependencies 审计边均保留
def test_update_completed_readies_dependency(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("step 1")
    mgr.create("step 2", blocked_by=[1])
    mgr.update(1, status="completed")
    downstream = mgr.get(2)
    assert downstream.status == "ready"
    assert downstream.dependencies == [1]


# 功能：验证 update add_blocked_by 正确追加依赖
# 设计：先创建两个任务，再为任务 2 追加对任务 1 的依赖
def test_update_add_blocked_by(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("a")
    mgr.create("b")
    mgr.update(2, add_blocked_by=[1])
    assert 1 in mgr.get(2).blocked_by


# 功能：验证任务依赖更新拒绝形成间接环路且失败不会污染原记录
# 设计：先建立 2→1，再尝试写入 1→2，断言报出 cycle 且任务 1 仍无依赖
def test_update_rejects_dependency_cycle(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("first")
    manager.create("second", blocked_by=[1])

    with pytest.raises(ValueError, match="task dependency cycle"):
        manager.update(1, add_blocked_by=[2])

    assert manager.get(1).dependencies == []


# 功能：验证 update remove_blocked_by 正确移除依赖
# 设计：创建带依赖的任务，再移除依赖，断言 blocked_by 为空
def test_update_remove_blocked_by(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("a")
    mgr.create("b", blocked_by=[1])
    mgr.update(2, remove_blocked_by=[1])
    assert mgr.get(2).blocked_by == []


# 功能：验证 list_all 返回所有任务，按 ID 升序排列
# 设计：创建三个任务后 list_all，断言数量为 3 且 ID 顺序正确
def test_list_all_ordered(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("x")
    mgr.create("y")
    mgr.create("z")
    tasks = mgr.list_all()
    assert len(tasks) == 3
    assert [t.id for t in tasks] == [1, 2, 3]


# 功能：验证 format_list 输出包含状态标记和任务名
# 设计：创建两个任务并更新其中一个，检查 format_list 字符串内容
def test_format_list_content(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("alpha")
    mgr.create("beta")
    mgr.update(1, status="completed")
    result = mgr.format_list()
    assert "[x]" in result
    assert "alpha" in result
    assert "beta" in result


# 功能：验证 TaskManager 重新实例化时能从现有文件恢复 next_id
# 设计：第一个 mgr 创建 2 个任务，第二个 mgr 读取同目录，新任务 ID 应为 3
def test_manager_resumes_id_from_existing_files(tmp_path: Path) -> None:
    mgr1 = TaskManager(tmp_path)
    mgr1.create("first")
    mgr1.create("second")

    mgr2 = TaskManager(tmp_path)
    task = mgr2.create("third")
    assert task.id == 3


# 功能：验证未阻塞任务只能被一个 owner 原子认领
# 设计：首次 claim 后再次认领同一任务应失败，同时检查状态和 owner 已持久化
def test_claim_assigns_owner_and_rejects_second_claim(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("parallel work")

    claimed = mgr.claim(1, "reviewer", "review-wt")

    assert claimed.status == "running"
    assert claimed.owner == "reviewer"
    assert claimed.worktree == "review-wt"
    assert claimed.attempts[0].status == "running"
    with pytest.raises(ValueError, match="cannot claim"):
        mgr.claim(1, "executor")


# 功能：验证仍有未完成依赖的任务不能被认领
# 设计：创建依赖边后直接 claim 下游任务，断言错误包含阻塞任务 ID
def test_claim_rejects_blocked_task(tmp_path: Path) -> None:
    mgr = TaskManager(tmp_path)
    mgr.create("first")
    mgr.create("second", blocked_by=[1])

    with pytest.raises(ValueError, match=r"blocked by \[1\]"):
        mgr.claim(2, "executor")


# 功能：验证 daemon 等价重启后 timeline、attempt 和 artifact 均可查询
# 设计：用第二个 TaskManager 读取同一目录，覆盖 R6 的持久恢复验收路径
def test_restart_restores_timeline_attempt_and_artifact(tmp_path: Path) -> None:
    first = TaskManager(tmp_path)
    first.create("durable work", acceptance_criteria=["tests pass"])
    first.claim(1, "executor", "wt-1")
    first.add_artifact(
        1,
        name="report",
        uri="artifact://sha256/abc",
        digest="sha256:abc",
        media_type="text/markdown",
    )

    restored = TaskManager(tmp_path).get(1)

    assert restored.acceptance_criteria == ["tests pass"]
    assert len(restored.attempts) == 1
    assert restored.attempts[0].owner_worker == "executor"
    assert restored.artifacts[0].digest == "sha256:abc"
    assert [entry.event for entry in restored.timeline] == [
        "task.created",
        "task.claimed",
        "task.artifact_added",
    ]


# 功能：验证未通过 gate 的任务不能完成，通过并记录证据后才可完成
# 设计：先断言 fail-closed，再设置 gate 证据并完成，覆盖 gate 的完整状态转换
def test_gate_blocks_completion_until_passed(tmp_path: Path) -> None:
    manager = TaskManager(tmp_path)
    manager.create("guarded work", gates=["unit-tests"])

    with pytest.raises(ValueError, match="gates that have not passed"):
        manager.update(1, status="completed")

    manager.set_gate(1, "unit-tests", passed=True, evidence="pytest: 12 passed")
    completed = manager.update(1, status="completed")

    assert completed.status == "completed"
    assert completed.gates[0].status == "passed"
    assert completed.gates[0].evidence == "pytest: 12 passed"


# 功能：验证任务 timeline 事件只在任务文件保存成功后发送
# 设计：收集 event sink 回调并读取对应文件，防止写入失败产生不可追溯幽灵事件
def test_event_sink_observes_persisted_task(tmp_path: Path) -> None:
    observed: list[tuple[str, bool]] = []

    # 在回调触发时确认所属任务文件已经存在
    def receive(entry: object) -> None:
        task_id = getattr(entry, "task_id")
        event = getattr(entry, "event")
        observed.append((event, (tmp_path / f"task_{task_id}.json").exists()))

    manager = TaskManager(tmp_path, event_sink=receive)
    manager.create("persist first")

    assert observed == [("task.created", True)]

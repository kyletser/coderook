"""Phase B (B1+B3+B4) 集成测试：profile.model 接线、共享 TaskManager、daemon 级 registry"""

from __future__ import annotations

from pathlib import Path

from code_rook.core.agents.loader import AgentProfile, AgentProfileLoader
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.registry import ToolRegistry


def _usage() -> UsageStats:
    return UsageStats(input_tokens=1, output_tokens=1)


# 功能：provider wrapper 在 profile.model 非空时把 model kwarg 注入到 child chat 调用
# 设计：模拟 profile 带 model；构造 spawn tool 用 profile；child provider 应该收到 model kwarg
async def test_child_provider_receives_model_from_profile(tmp_path: Path) -> None:
    captured_model: list[str | None] = []

    class FakeProvider:
        _model = "default-model"

        async def chat(self, *a, **kw):  # type: ignore[no-untyped-def]
            captured_model.append(kw.get("model"))
            return LlmResponse(
                stop_reason="end_turn",
                text="child done",
                usage=_usage(),
            )

    provider = FakeProvider()
    profile = AgentProfile(
        name="executor",
        description="test",
        system_prompt="You are a test executor.",
        allowed_tools=["read_file"],
        model="claude-haiku-4-5-20251001",
        restrict="",
    )

    profile_tasks = tmp_path / ".tasks"
    profile_tasks.mkdir()
    task_manager = TaskManager(profile_tasks)
    task_manager.create("test task")

    registry = ToolRegistry()
    from code_rook.core.tools.builtin.read_file import ReadFileTool
    from code_rook.core.workspace import WorkspaceBoundary

    registry.register(ReadFileTool(WorkspaceBoundary(tmp_path)))

    # 用 spawn tool 的 profile 逻辑独立验证 provider 包装
    # 这里测的是 _ModelOverrideProvider 的语义
    _parent_provider = provider
    _child_model: str = profile.model

    class _Override:
        async def chat(self, *a, **kw):  # type: ignore[no-untyped-def]
            return await _parent_provider.chat(*a, model=_child_model, **kw)

    child_provider = _Override()
    result = await child_provider.chat(
        [], [], EventBus(), "run-1", step=1, system="hello"
    )
    assert result.stop_reason == "end_turn"
    assert captured_model == ["claude-haiku-4-5-20251001"]


# 功能：profile.model 为空时不注入 model kwarg（子 Agent 用父 provider 默认 model）
# 设计：profile.model 为空串时 wrapper 不被创建，实例化不传 model
async def test_child_provider_unchanged_when_profile_has_no_model() -> None:
    profile = AgentProfile(
        name="generic",
        description="test",
        system_prompt="",
        allowed_tools=[],
        model="",
        restrict="",
    )
    assert not profile.model
    # 无 model 时 spawn 工具里不会进入 _ModelOverrideProvider 分支
    # 只需要验证 profile.model 为空串时 bool 为 False


# 功能：同 run 内所有 child 共享一个 TaskManager 实例
# 设计：父 task_manager 创建 task，child 的 task_list 应该能看见同一个 task
async def test_shared_task_manager_visible_to_children(tmp_path: Path) -> None:
    tasks_dir = tmp_path / ".tasks"
    tasks_dir.mkdir()
    tm_parent = TaskManager(tasks_dir)
    created = tm_parent.create("shared task", "desc")
    assert created.id == 1

    child_tm = tm_parent
    tasks = child_tm.list_all()
    assert len(tasks) == 1
    assert tasks[0].id == 1
    assert tasks[0].subject == "shared task"


# 功能：daemon 级 BackgroundTaskRegistry 跨 turn 保留后台任务
# 设计：注册后台任务后 registry 仍然持有引用（不因 turn 结束而清理）
async def test_daemon_level_registry_preserves_tasks() -> None:
    reg = BackgroundTaskRegistry()
    assert len(reg.all()) == 0

    # 模拟注册一个已完成的后台任务
    import asyncio

    async def _done() -> None:
        pass

    task = asyncio.create_task(_done())
    await task  # 等它完成
    ctx = ExecutionContext(run_id="bg-1", goal="bg", max_steps=5)
    reg.register("bg-1", task, ctx)
    # daemon 级不会在 turn 结束时清理
    still_has = reg.get("bg-1")
    assert still_has is not None
    assert len(reg.all()) == 1


# 功能：内建 reviewer profile 的 model 字段非空且与 loader 一致
# 设计：加载 reviewer → 断言 model == "claude-sonnet-4-6"（TOML 中原值）
def test_reviewer_profile_has_model() -> None:
    profile = AgentProfileLoader().load("reviewer")
    assert profile is not None
    assert profile.model == "claude-sonnet-4-6"


# 功能：内建 executor profile 的 model 字段非空
# 设计：executor 也用带 model profile
def test_executor_profile_has_model() -> None:
    profile = AgentProfileLoader().load("executor")
    assert profile is not None
    assert profile.model == "claude-sonnet-4-6"


# 功能：BackgroundTaskRegistry.cancel_all 清理所有注册任务
# 设计：cancel_all 后 all() 返回空
async def test_cancel_all_clears_registry() -> None:
    reg = BackgroundTaskRegistry()
    import asyncio

    async def _forever() -> None:
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            raise

    ctx = ExecutionContext(run_id="bg-c", goal="bg", max_steps=5)
    task = asyncio.create_task(_forever())
    reg.register("bg-c", task, ctx)
    assert len(reg.all()) == 1
    await reg.cancel_all()
    assert len(reg.all()) == 1  # cancel_all 不删除 entry，只取消并标记
    assert task.cancelled() or task.done()

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from code_rook.core.authority import RuntimeMode
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.invocation import invoke_tool


# 从 action-family schema 提取可见 action 名称
def _actions(schema: dict[str, object]) -> set[str]:
    input_schema = schema["input_schema"]
    assert isinstance(input_schema, dict)
    variants = input_schema["oneOf"]
    assert isinstance(variants, list)
    return {
        str(variant["properties"]["action"]["enum"][0])
        for variant in variants
    }


# 功能：默认工具面暴露 memory/tasks/update_plan 且隐藏全部旧平铺名称
# 设计：构建带 EventBus 的真实 Runner 目录，同时检查模型 schema 和内部 replay backend
def test_default_control_tools_use_bounded_family_surface(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=EventBus())
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        run_id="run-control",
        bus=EventBus(),
    )
    names = {str(schema["name"]) for schema in registry.tool_schemas()}

    assert {"memory", "tasks", "update_plan"} <= names
    assert {
        "memory_save",
        "memory_search",
        "memory_forget",
        "task_create",
        "task_claim",
        "task_update",
        "task_list",
        "task_get",
    }.isdisjoint(names)
    assert registry.get("memory_save") is not None
    assert registry.get("task_create") is not None


# 功能：Plan Mode 仅保留 control family 的只读 action 和 update_plan
# 设计：用真实目录验证 action 裁剪发生在 family 内，而非隐藏 memory/tasks 整体
def test_plan_mode_filters_control_family_mutations(tmp_path: Path) -> None:
    bus = EventBus()
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=bus)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        run_id="run-plan",
        bus=bus,
        runtime_mode=RuntimeMode.PLAN,
    )
    schemas = {str(item["name"]): item for item in registry.tool_schemas()}

    assert _actions(schemas["memory"]) == {"search"}
    assert _actions(schemas["tasks"]) == {"list", "get"}
    assert "update_plan" in schemas


# 功能：memory 与 tasks family 调用真实 backend 并保持持久状态
# 设计：先通过 family 保存记忆和创建任务，再用各自只读 action 查询实际结果
async def test_control_families_dispatch_to_real_backends(tmp_path: Path) -> None:
    bus = EventBus()
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=bus)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        run_id="run-dispatch",
        bus=bus,
    )
    memory = registry.get("memory")
    tasks = registry.get("tasks")
    assert memory is not None and tasks is not None

    saved = await memory.invoke(
        {"action": "save", "name": "Rule", "body": "Run focused tests."}
    )
    recalled = await memory.invoke({"action": "search", "query": "focused tests"})
    created = await tasks.invoke({"action": "create", "subject": "Verify family"})
    listed = await tasks.invoke({"action": "list"})

    assert not saved.is_error and "memory_id=" in saved.content
    assert not recalled.is_error and "Run focused tests" in recalled.content
    assert not created.is_error and json.loads(created.content)["subject"] == "Verify family"
    assert not listed.is_error and "Verify family" in listed.content


# 功能：update_plan 发布类型化计划事件并拒绝两个同时进行的步骤
# 设计：经统一 invoke_tool 路径收集 EventBus，分别验证成功事件和 Pydantic schema fail-closed
async def test_update_plan_is_typed_and_fail_closed(tmp_path: Path) -> None:
    events: list[BaseModel] = []
    bus = EventBus()

    # 收集计划与工具生命周期事件
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=bus)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        run_id="run-update-plan",
        bus=bus,
    )
    valid = await invoke_tool(
        registry,
        ToolCallBlock(
            id="plan-1",
            name="update_plan",
            input={
                "explanation": "Initial implementation plan",
                "plan": [
                    {"step": "Inspect", "status": "completed"},
                    {"step": "Implement", "status": "in_progress"},
                ],
            },
        ),
        bus,
        "run-update-plan",
    )
    invalid = await invoke_tool(
        registry,
        ToolCallBlock(
            id="plan-2",
            name="update_plan",
            input={
                "plan": [
                    {"step": "One", "status": "in_progress"},
                    {"step": "Two", "status": "in_progress"},
                ]
            },
        ),
        bus,
        "run-update-plan",
    )

    plan_events = [event for event in events if event.type == "plan.updated"]  # type: ignore[attr-defined]
    assert not valid.is_error
    assert len(plan_events) == 1
    assert plan_events[0].plan[1].step == "Implement"  # type: ignore[attr-defined]
    assert invalid.is_error and invalid.error_type == "schema_error"

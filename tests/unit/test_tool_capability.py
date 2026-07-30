from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.tool import SpawnAgentTool
from code_rook.core.tools.base import BaseTool, ToolSideEffect


# 功能：BaseTool 默认安全保守——side_effect=EXTERNAL_WRITE、can_parallel=False、is_read_only=False
# 设计：构造一个未显式声明的工具类，断言默认值与设计一致，避免新增工具不显式标注被视为低害
def test_base_tool_defaults_are_conservative() -> None:
    class _Bare(BaseTool):  # noqa: D401
        name = "bare"
        description = "no capability declared"
        input_schema: dict[str, object] = {
            "type": "object", "properties": {}, "required": []
        }

        async def invoke(self, params: dict[str, object]) -> Any: ...  # pragma: no cover

    t = _Bare()
    assert t.side_effect == ToolSideEffect.EXTERNAL_WRITE
    assert t.can_parallel is False
    assert t.is_read_only is False
    assert t.retry_policy.name == "NEVER"


# 功能：纯读工具 is_read_only 反映 side_effect==NONE；can_parallel=True 显式声明
# 设计：直接断言 ReadFileTool / GlobTool / GrepTool 的关键字段，避免 capability 被漂移
def test_read_only_tools_capability() -> None:
    from code_rook.core.tools.builtin.git_diff import GitDiffTool
    from code_rook.core.tools.builtin.glob import GlobTool
    from code_rook.core.tools.builtin.grep import GrepTool
    from code_rook.core.tools.builtin.list_dir import ListDirTool
    from code_rook.core.tools.builtin.read_file import ReadFileTool

    for cls in (ReadFileTool, GlobTool, GrepTool, ListDirTool, GitDiffTool):
        assert cls.side_effect == ToolSideEffect.NONE, cls.__name__
        assert cls.can_parallel is True, cls.__name__
        # is_read_only 依赖 side_effect，类常量阶段可读
        assert cls.is_read_only.fget is not None  # type: ignore[attr-defined]


# 功能：本地写入工具显式 side_effect=LOCAL_WRITE，is_read_only 为 False
# 设计：这三个工具是本地文件系统原子写入，side_effect 必须区别于纯读和外部写
def test_local_write_tools_capability() -> None:
    from code_rook.core.tools.builtin.apply_patch import ApplyPatchTool
    from code_rook.core.tools.builtin.edit_file import EditFileTool
    from code_rook.core.tools.builtin.write_file import WriteFileTool

    # side_effect 是 ClassVar，类层级可直接读取
    assert EditFileTool.side_effect == ToolSideEffect.LOCAL_WRITE
    assert WriteFileTool.side_effect == ToolSideEffect.LOCAL_WRITE
    assert ApplyPatchTool.side_effect == ToolSideEffect.LOCAL_WRITE


# 功能：reviewer 角色应用 restrict=read_only 后子 registry 不应包含任何写工具
# 设计：用真实 SpawnAgentTool._build_child_registry 派生 reviewer 的 registry，
#       断言 bash/edit_file/write_file/apply_patch 不存在，read_file/glob/grep 存在
def _make_spawn_tool(tmp_path: Path) -> SpawnAgentTool:
    provider = AsyncMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text="ok",
            usage=UsageStats(
                input_tokens=0, output_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=0.0,
            ),
        )
    )
    return SpawnAgentTool(
        provider=provider,
        parent_bus=EventBus(),
        parent_run_id="r-test",
        permission_manager=None,
        max_steps=3,
        task_registry=BackgroundTaskRegistry(),
        runs_dir=tmp_path,
        session_id="sess-test",
        depth=0,
        workspace_boundary=None,
        task_manager=None,
    )


# 功能：reviewer 角色子 registry 排除所有副作用工具，保留只读工具
# 设计：手工构造 reviewer AgentProfile(restrict=read_only, allowed_tools=[])，
#       调 _build_child_registry，断言命名集合与 capability 模型一致
def test_reviewer_restrict_excludes_write_tools(tmp_path: Path) -> None:
    from code_rook.core.agents.loader import AgentProfile
    from code_rook.core.workspace import WorkspaceBoundary

    tool = _make_spawn_tool(tmp_path)
    profile = AgentProfile(
        name="reviewer",
        description="read-only reviewer",
        system_prompt="",
        allowed_tools=[],
        model="",
        restrict="read_only",
    )
    boundary = WorkspaceBoundary(tmp_path)
    registry = tool._build_child_registry(
        child_bus=EventBus(),
        child_run_id="r-reviewer",
        profile=profile,
        boundary=boundary,
        task_manager=None,
    )
    names = set()

    # 遍历注册表内部字典以收集工具名（registry 没有公开 names API）
    for name in registry._tools.keys():  # type: ignore[attr-defined]
        names.add(name)

    # 严格不允许的副作用工具
    assert "bash" not in names
    assert "edit_file" not in names
    assert "write_file" not in names
    assert "apply_patch" not in names
    assert "checkpoint_rewind" not in names
    assert "task_create" not in names
    assert "task_claim" not in names
    assert "task_update" not in names
    assert "spawn_agent" not in names

    # 必备只读工具
    assert "read_file" in names
    assert "glob" in names
    assert "grep" in names
    assert "list_dir" in names
    assert "git_diff" in names
    assert "checkpoint_list" in names
    assert "task_list" in names
    assert "task_get" in names
    # agent_result 查询后台完成状态，无副作用，应在只读范围内
    assert "agent_result" in names


# 功能：restrict 与显式 allowed_tools 同时存在时取最严子集
# 设计：profile 同时声明 restrict=read_only 与 allowed_tools 含 bash，
#       断言 bash 仍被排除（restricted capability 优先），只读工具仍允许
def test_restrict_takes_strict_subset_over_allowed_tools(tmp_path: Path) -> None:
    from code_rook.core.agents.loader import AgentProfile
    from code_rook.core.workspace import WorkspaceBoundary

    tool = _make_spawn_tool(tmp_path)
    profile = AgentProfile(
        name="broken_reviewer",
        description="explicit allowed contains bash but restrict must win",
        system_prompt="",
        allowed_tools=["bash", "read_file", "spawn_agent"],
        model="",
        restrict="read_only",
    )
    boundary = WorkspaceBoundary(tmp_path)
    registry = tool._build_child_registry(
        child_bus=EventBus(),
        child_run_id="r-broken",
        profile=profile,
        boundary=boundary,
        task_manager=None,
    )
    names = set(registry._tools.keys())  # type: ignore[attr-defined]
    assert "bash" not in names  # restrict 排除 bash
    assert "spawn_agent" not in names  # restrict 排除 spawn
    assert "read_file" in names  # 仍在显式名单里且符合 restrict


# 功能：未设置 restrict 时，显式 allowed_tools 决定可见工具
# 设计：profile 无 restrict，allowed_tools=["read_file","glob"]，
#       断言具见到 read_file+glob 但没有其它（包括 list_dir）
def test_allowed_tools_alone_filters_by_name(tmp_path: Path) -> None:
    from code_rook.core.agents.loader import AgentProfile
    from code_rook.core.workspace import WorkspaceBoundary

    tool = _make_spawn_tool(tmp_path)
    profile = AgentProfile(
        name="min",
        description="explicit name allow-list without restrict",
        system_prompt="",
        allowed_tools=["read_file", "glob"],
        model="",
        restrict="",
    )
    boundary = WorkspaceBoundary(tmp_path)
    registry = tool._build_child_registry(
        child_bus=EventBus(),
        child_run_id="r-min",
        profile=profile,
        boundary=boundary,
        task_manager=None,
    )
    names = set(registry._tools.keys())  # type: ignore[attr-defined]
    assert "read_file" in names
    assert "glob" in names
    assert "list_dir" not in names
    assert "bash" not in names


# 功能：profile=None 时无任何过滤，所有可能工具注册
# 设计：profile=None，断言 bash/edit_file/apply_patch 都进入 registry
def test_no_profile_allows_all_tools(tmp_path: Path) -> None:
    from code_rook.core.workspace import WorkspaceBoundary

    tool = _make_spawn_tool(tmp_path)
    boundary = WorkspaceBoundary(tmp_path)
    registry = tool._build_child_registry(
        child_bus=EventBus(),
        child_run_id="r-none",
        profile=None,
        boundary=boundary,
        task_manager=None,
    )
    names = set(registry._tools.keys())  # type: ignore[attr-defined]
    assert "bash" in names
    assert "edit_file" in names
    assert "apply_patch" in names
    assert "read_file" in names